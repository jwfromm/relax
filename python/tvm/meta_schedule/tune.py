# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""User-facing Tuning API"""

import logging
import os.path
from typing import Callable, Dict, List, Optional, Union

import tvm
from tvm import relay
from tvm._ffi.registry import register_func
from tvm.relay import Function as RelayFunc
from tvm.relay.backend.executor_factory import ExecutorFactoryModule
from tvm.ir.base import structural_equal, structural_hash
from tvm.ir.module import IRModule
from tvm.runtime import NDArray
from tvm.target.target import Target
from tvm.te import Tensor, create_prim_func
from tvm.tir import PrimFunc, Schedule

from .integration import extract_task_from_relay, ApplyHistoryBest
from .builder import Builder, LocalBuilder
from .cost_model import CostModel, XGBModel
from .database import Database, JSONDatabase, TuningRecord
from .feature_extractor import PerStoreFeature
from .measure_callback import MeasureCallback
from .mutator import Mutator
from .postproc import Postproc
from .runner import LocalRunner, Runner
from .schedule_rule import ScheduleRule
from .search_strategy import (
    EvolutionarySearchConfig,
    ReplayFuncConfig,
    ReplayTraceConfig,
)
from .space_generator import PostOrderApply, SpaceGenerator
from .task_scheduler import RoundRobin, TaskScheduler
from .tune_context import TuneContext


logger = logging.getLogger(__name__)  # pylint: disable=invalid-name

SearchStrategyConfig = Union[
    ReplayFuncConfig,
    ReplayTraceConfig,
    EvolutionarySearchConfig,
]
TypeSpaceGenerator = Callable[[], SpaceGenerator]
TypeScheduleRule = Callable[[], List[ScheduleRule]]
TypePostproc = Callable[[], List[Postproc]]
TypeMutatorProb = Callable[[], Dict[Mutator, float]]
TypeTaskScheduler = Callable[
    [
        List[TuneContext],
        Builder,
        Runner,
        Database,
        CostModel,
        List[MeasureCallback],
    ],
    TaskScheduler,
]


class DefaultLLVM:
    """Default tuning configuration for LLVM."""

    @staticmethod
    def _sch_rules() -> List[ScheduleRule]:
        from tvm.meta_schedule import (  # pylint: disable=import-outside-toplevel
            schedule_rule as M,
        )

        return [
            M.AutoInline(
                into_producer=False,
                into_consumer=True,
                # into_cache_only=False, # TODO(@automation): Update the AutoInline
                inline_const_tensor=True,
                disallow_if_then_else=True,
                require_injective=True,
                require_ordered=True,
                disallow_op=["tir.exp"],
            ),
            M.AddRFactor(max_jobs_per_core=16, max_innermost_factor=64),
            M.MultiLevelTiling(
                structure="SSRSRS",
                tile_binds=None,
                max_innermost_factor=64,
                vector_load_lens=None,
                reuse_read=None,
                reuse_write=M.ReuseType(
                    req="may",
                    levels=[1, 2],
                    scope="global",
                ),
            ),
            M.ParallelizeVectorizeUnroll(
                max_jobs_per_core=16,
                max_vectorize_extent=64,
                unroll_max_steps=[0, 16, 64, 512],
                unroll_explicit=True,
            ),
            M.RandomComputeLocation(),
        ]

    @staticmethod
    def _postproc() -> List[Postproc]:
        from tvm.meta_schedule import (  # pylint: disable=import-outside-toplevel
            postproc as M,
        )

        return [
            M.DisallowDynamicLoop(),
            M.RewriteParallelVectorizeUnroll(),
            M.RewriteReductionBlock(),
        ]

    @staticmethod
    def _mutator_probs() -> Dict[Mutator, float]:
        from tvm.meta_schedule import (  # pylint: disable=import-outside-toplevel
            mutator as M,
        )

        return {
            # TODO(@automation): Upstream the mutators
            # M.MutateTileSize(): 0.9,
            M.MutateComputeLocation(): 0.05,
            M.MutateUnroll(): 0.03,
            # M.MutateParallel(max_jobs_per_core=16): 0.02,
        }


class DefaultCUDA:
    """Default tuning configuration for CUDA."""

    @staticmethod
    def _sch_rules() -> List[ScheduleRule]:
        from tvm.meta_schedule import (  # pylint: disable=import-outside-toplevel
            schedule_rule as M,
        )

        return [
            M.MultiLevelTiling(
                structure="SSSRRSRS",
                tile_binds=["blockIdx.x", "vthread.x", "threadIdx.x"],
                max_innermost_factor=64,
                vector_load_lens=[1, 2, 3, 4],
                reuse_read=M.ReuseType(
                    req="must",
                    levels=[4],
                    scope="shared",
                ),
                reuse_write=M.ReuseType(
                    req="must",
                    levels=[3],
                    scope="local",
                ),
            ),
            M.AutoInline(
                into_producer=True,
                into_consumer=True,
                # into_cache_only=False,
                inline_const_tensor=True,
                disallow_if_then_else=False,
                require_injective=False,
                require_ordered=False,
                disallow_op=None,
            ),
            M.CrossThreadReduction(thread_extents=[4, 8, 16, 32, 64, 128, 256, 512]),
            M.ParallelizeVectorizeUnroll(
                max_jobs_per_core=-1,  # disable parallelize
                max_vectorize_extent=-1,  # disable vectorize
                unroll_max_steps=[0, 16, 64, 512, 1024],
                unroll_explicit=True,
            ),
        ]

    @staticmethod
    def _postproc() -> List[Postproc]:
        from tvm.meta_schedule import (  # pylint: disable=import-outside-toplevel
            postproc as M,
        )

        return [
            M.DisallowDynamicLoop(),
            # TODO(@automation): Upstream the RewriteCooperativeFetch postproc
            # M.RewriteCooperativeFetch(),
            M.RewriteUnboundBlock(),
            M.RewriteParallelVectorizeUnroll(),
            M.RewriteReductionBlock(),
            M.VerifyGPUCode(),
        ]

    @staticmethod
    def _mutator_probs() -> Dict[Mutator, float]:
        from tvm.meta_schedule import (  # pylint: disable=import-outside-toplevel
            mutator as M,
        )

        return {
            # M.MutateTileSize(): 0.9,
            M.MutateUnroll(): 0.1,
        }


class Parse:
    """Parse tuning configuration from user inputs."""

    @staticmethod
    @register_func("tvm.meta_schedule.tune.parse_mod")  # for use in ApplyHistoryBest
    def _mod(mod: Union[PrimFunc, IRModule]) -> IRModule:
        if isinstance(mod, PrimFunc):
            mod = mod.with_attr("global_symbol", "main")
            mod = mod.with_attr("tir.noalias", True)
            mod = IRModule({"main": mod})
        if not isinstance(mod, IRModule):
            raise TypeError(f"Expected `mod` to be PrimFunc or IRModule, but gets: {mod}")
        # in order to make sure the mod can be found in ApplyHistoryBest
        # different func name can cause structural unequal
        func_names = mod.get_global_vars()
        (func_name,) = func_names
        if len(func_names) == 1 and func_name != "main":
            mod = IRModule({"main": mod[func_name]})
        return mod

    @staticmethod
    def _target(target: Union[str, Target]) -> Target:
        if isinstance(target, str):
            target = Target(target)
        if not isinstance(target, Target):
            raise TypeError(f"Expected `target` to be str or Target, but gets: {target}")
        return target

    @staticmethod
    def _builder(builder: Optional[Builder]) -> Builder:
        if builder is None:
            builder = LocalBuilder()
        if not isinstance(builder, Builder):
            raise TypeError(f"Expected `builder` to be Builder, but gets: {builder}")
        return builder

    @staticmethod
    def _runner(runner: Optional[Runner]) -> Runner:
        if runner is None:
            runner = LocalRunner()
        if not isinstance(runner, Runner):
            raise TypeError(f"Expected `runner` to be Runner, but gets: {runner}")
        return runner

    @staticmethod
    def _database(database: Union[None, Database], task_name: str, path: str) -> Database:
        if database is None:
            path_workload = os.path.join(path, f"{task_name}_database_workload.json")
            path_tuning_record = os.path.join(path, f"{task_name}_database_tuning_record.json")
            logger.info(
                "Creating JSONDatabase. Workload at: %s. Tuning records at: %s",
                path_workload,
                path_tuning_record,
            )
            database = JSONDatabase(
                path_workload=path_workload,
                path_tuning_record=path_tuning_record,
            )
        if not isinstance(database, Database):
            raise TypeError(f"Expected `database` to be Database, but gets: {database}")
        return database

    @staticmethod
    def _callbacks(
        measure_callbacks: Optional[List[MeasureCallback]],
    ) -> List[MeasureCallback]:
        if measure_callbacks is None:
            from tvm.meta_schedule import (  # pylint: disable=import-outside-toplevel
                measure_callback as M,
            )

            return [
                M.AddToDatabase(),
                M.RemoveBuildArtifact(),
                M.EchoStatistics(),
                M.UpdateCostModel(),
            ]
        if not isinstance(measure_callbacks, (list, tuple)):
            raise TypeError(
                f"Expected `measure_callbacks` to be List[MeasureCallback], "
                f"but gets: {measure_callbacks}"
            )
        measure_callbacks = list(measure_callbacks)
        for i, callback in enumerate(measure_callbacks):
            if not isinstance(callback, MeasureCallback):
                raise TypeError(
                    f"Expected `measure_callbacks` to be List[MeasureCallback], "
                    f"but measure_callbacks[{i}] is: {callback}"
                )
        return measure_callbacks

    @staticmethod
    def _cost_model(cost_model: Optional[CostModel]) -> CostModel:
        if cost_model is None:
            return XGBModel(extractor=PerStoreFeature())
        if not isinstance(cost_model, CostModel):
            raise TypeError(f"Expected `cost_model` to be CostModel, but gets: {cost_model}")
        return cost_model

    @staticmethod
    def _space_generator(space_generator: Optional[TypeSpaceGenerator]) -> SpaceGenerator:
        if space_generator is None:
            return PostOrderApply()
        if callable(space_generator):
            space_generator = space_generator()
        if not isinstance(space_generator, SpaceGenerator):
            raise TypeError(
                f"Expected `space_generator` to return SpaceGenerator, "
                f"but gets: {space_generator}"
            )
        return space_generator

    @staticmethod
    def _sch_rules(sch_rules: Optional[TypeScheduleRule], target: Target) -> List[ScheduleRule]:
        if callable(sch_rules):
            return sch_rules()
        if sch_rules is not None:
            raise TypeError(f"Expected `sch_rules` to be None or callable, but gets: {sch_rules}")
        # pylint: disable=protected-access
        if target.kind.name == "llvm":
            return DefaultLLVM._sch_rules()
        if target.kind.name == "cuda":
            return DefaultCUDA._sch_rules()
        # pylint: enable=protected-access
        raise ValueError(f"Unsupported target: {target}")

    @staticmethod
    def _postproc(postproc: Optional[TypePostproc], target: Target) -> List[Postproc]:
        if callable(postproc):
            return postproc()
        if postproc is not None:
            raise TypeError(f"Expected `postproc` to be None or callable, but gets: {postproc}")
        # pylint: disable=protected-access
        if target.kind.name == "llvm":
            return DefaultLLVM._postproc()
        if target.kind.name == "cuda":
            return DefaultCUDA._postproc()
        # pylint: enable=protected-access
        raise ValueError(f"Unsupported target: {target}")

    @staticmethod
    def _mutator_probs(
        mutator_probs: Optional[TypeMutatorProb],
        target: Target,
    ) -> Dict[Mutator, float]:
        if callable(mutator_probs):
            return mutator_probs()
        if mutator_probs is not None:
            raise TypeError(
                f"Expected `mutator_probs` to be None or callable, but gets: {mutator_probs}"
            )
        # pylint: disable=protected-access
        if target.kind.name == "llvm":
            return DefaultLLVM._mutator_probs()
        if target.kind.name == "cuda":
            return DefaultCUDA._mutator_probs()
        # pylint: enable=protected-access
        raise ValueError(f"Unsupported target: {target}")

    @staticmethod
    def _tune_context(
        tune_context: Optional[TuneContext],
        mod: IRModule,
        target: Target,
        config: SearchStrategyConfig,
        task_name: str,
        space_generator: Optional[TypeSpaceGenerator],
        sch_rules: Optional[TypeScheduleRule],
        postprocs: Optional[TypePostproc],
        mutator_probs: Optional[TypeMutatorProb],
        num_threads: Optional[int],
    ) -> TuneContext:
        if tune_context is None:
            return TuneContext(
                mod=mod,
                target=target,
                # pylint: disable=protected-access
                space_generator=Parse._space_generator(space_generator),
                search_strategy=config.create_strategy(),
                sch_rules=Parse._sch_rules(sch_rules, target),
                postprocs=Parse._postproc(postprocs, target),
                mutator_probs=Parse._mutator_probs(mutator_probs, target),
                # pylint: enable=protected-access
                task_name=task_name,
                rand_state=-1,
                num_threads=num_threads,
            )
        if not isinstance(tune_context, TuneContext):
            raise TypeError(f"Expected `tune_context` to be TuneContext, but gets: {tune_context}")
        return tune_context

    @staticmethod
    def _task_scheduler(
        task_scheduler: Union[None, TaskScheduler, TypeTaskScheduler],
        tasks: List[TuneContext],
        builder: Builder,
        runner: Runner,
        database: Database,
        cost_model: CostModel,
        measure_callbacks: List[MeasureCallback],
    ):
        if task_scheduler is None:
            return RoundRobin(
                tasks=tasks,
                builder=builder,
                runner=runner,
                database=database,
                cost_model=cost_model,
                measure_callbacks=measure_callbacks,
            )
        if callable(task_scheduler):
            return task_scheduler(
                tasks,
                builder,
                runner,
                database,
                cost_model,
                measure_callbacks,
            )
        if not isinstance(task_scheduler, TaskScheduler):
            raise TypeError(
                f"Expected `task_scheduler` to be TaskScheduler, but gets: {task_scheduler}"
            )
        return task_scheduler


def tune_tir(
    mod: Union[IRModule, PrimFunc],
    target: Union[str, Target],
    config: SearchStrategyConfig,
    work_dir: str,
    *,
    task_name: str = "main",
    builder: Optional[Builder] = None,
    runner: Optional[Runner] = None,
    database: Optional[Database] = None,
    cost_model: Optional[CostModel] = None,
    measure_callbacks: Optional[List[MeasureCallback]] = None,
    task_scheduler: Optional[TaskScheduler] = None,
    space: Optional[TypeSpaceGenerator] = None,
    sch_rules: Optional[TypeScheduleRule] = None,
    postprocs: Optional[TypePostproc] = None,
    mutator_probs: Optional[TypeMutatorProb] = None,
    num_threads: Optional[int] = None,
) -> Optional[Schedule]:
    """Tune a TIR IRModule with a given target.

    Parameters
    ----------
    mod : Union[IRModule, PrimFunc]
        The module to tune.
    target : Union[str, Target]
        The target to tune for.
    config : SearchStrategyConfig
        The search strategy config.
    task_name : str
        The name of the task.
    work_dir : Optional[str]
        The working directory to save intermediate results.
    builder : Optional[Builder]
        The builder to use.
    runner : Optional[Runner]
        The runner to use.
    database : Optional[Database]
        The database to use.
    cost_model : Optional[CostModel]
        The cost model to use.
    measure_callbacks : Optional[List[MeasureCallback]]
        The callbacks used during tuning.
    f_tune_context : Optional[TYPE_F_TUNE_CONTEXT]
        The function to create TuneContext.
    f_task_scheduler : Optional[TYPE_F_TASK_SCHEDULER]
        The function to create TaskScheduler.

    Returns
    -------
    sch : Optional[Schedule]
        The tuned schedule.
    """

    logger.info("Working directory: %s", work_dir)
    # pylint: disable=protected-access
    mod = Parse._mod(mod)
    database = Parse._database(database, task_name, work_dir)
    tune_context = Parse._tune_context(
        tune_context=None,
        mod=mod,
        target=Parse._target(target),
        config=config,
        task_name=task_name,
        space_generator=space,
        sch_rules=sch_rules,
        postprocs=postprocs,
        mutator_probs=mutator_probs,
        num_threads=num_threads,
    )
    task_scheduler = Parse._task_scheduler(
        task_scheduler,
        [tune_context],
        builder=Parse._builder(builder),
        runner=Parse._runner(runner),
        database=database,
        cost_model=Parse._cost_model(cost_model),
        measure_callbacks=Parse._callbacks(measure_callbacks),
    )
    # pylint: enable=protected-access
    task_scheduler.tune()
    bests: List[TuningRecord] = database.get_top_k(
        database.commit_workload(mod),
        top_k=1,
    )
    if not bests:
        return None
    assert len(bests) == 1
    sch = Schedule(mod)
    bests[0].trace.apply_to_schedule(sch, remove_postproc=False)
    task_scheduler.cost_model.save(os.path.join(work_dir, f"{task_name}.xgb"))
    return sch


def tune_te(
    tensors: List[Tensor],
    target: Union[str, Target],
    config: SearchStrategyConfig,
    work_dir: str,
    *,
    task_name: str = "main",
    builder: Optional[Builder] = None,
    runner: Optional[Runner] = None,
    database: Optional[Database] = None,
    cost_model: Optional[CostModel] = None,
    measure_callbacks: Optional[List[MeasureCallback]] = None,
    task_scheduler: Optional[TaskScheduler] = None,
    space: Optional[TypeSpaceGenerator] = None,
    sch_rules: Optional[TypeScheduleRule] = None,
    postprocs: Optional[TypePostproc] = None,
    mutator_probs: Optional[TypeMutatorProb] = None,
    num_threads: Optional[int] = None,
) -> Optional[Schedule]:
    """Tune a TE compute DAG with a given target.

    Parameters
    ----------
    tensor : List[Tensor]
        The list of input/output tensors of the TE compute DAG.
    target : Union[str, Target]
        The target to tune for.
    config : SearchStrategyConfig
        The search strategy config.
    task_name : str
        The name of the task.
    work_dir : Optional[str]
        The working directory to save intermediate results.
    builder : Optional[Builder]
        The builder to use.
    runner : Optional[Runner]
        The runner to use.
    database : Optional[Database]
        The database to use.
    measure_callbacks : Optional[List[MeasureCallback]]
        The callbacks used during tuning.
    f_tune_context : Optional[TYPE_F_TUNE_CONTEXT]
        The function to create TuneContext.
    f_task_scheduler : Optional[TYPE_F_TASK_SCHEDULER]
        The function to create TaskScheduler.

    Returns
    -------
    sch : Optional[Schedule]
        The tuned schedule.
    """
    return tune_tir(
        mod=create_prim_func(tensors),
        target=target,
        config=config,
        work_dir=work_dir,
        task_name=task_name,
        builder=builder,
        runner=runner,
        database=database,
        cost_model=cost_model,
        measure_callbacks=measure_callbacks,
        task_scheduler=task_scheduler,
        space=space,
        sch_rules=sch_rules,
        postprocs=postprocs,
        mutator_probs=mutator_probs,
        num_threads=num_threads,
    )


def tune_relay(
    mod: Union[RelayFunc, IRModule],
    target: Union[str, Target],
    config: SearchStrategyConfig,
    work_dir: str,
    *,
    params: Optional[Dict[str, NDArray]] = None,
    task_name: str = "main",
    builder: Optional[Builder] = None,
    runner: Optional[Runner] = None,
    database: Optional[Database] = None,
    cost_model: Optional[CostModel] = None,
    measure_callbacks: Optional[List[MeasureCallback]] = None,
    task_scheduler: Optional[TaskScheduler] = None,
    space: Optional[TypeSpaceGenerator] = None,
    sch_rules: Optional[TypeScheduleRule] = None,
    postprocs: Optional[TypePostproc] = None,
    mutator_probs: Optional[TypeMutatorProb] = None,
    num_threads: Optional[int] = None,
) -> ExecutorFactoryModule:
    """Tune a TIR IRModule with a given target.

    Parameters
    ----------
    mod : Union[RelayFunc, IRModule]
        The module to tune.
    target : Union[str, Target]
        The target to tune for.
    config : SearchStrategyConfig
        The search strategy config.
    params : Optional[Dict[str, tvm.runtime.NDArray]]
        The associated parameters of the program
    task_name : str
        The name of the task.
    work_dir : Optional[str]
        The working directory to save intermediate results.
    builder : Optional[Builder]
        The builder to use.
    runner : Optional[Runner]
        The runner to use.
    database : Optional[Database]
        The database to use.
    measure_callbacks : Optional[List[MeasureCallback]]
        The callbacks used during tuning.
    f_tune_context : Optional[TYPE_F_TUNE_CONTEXT]
        The function to create TuneContext.
    f_task_scheduler : Optional[TYPE_F_TASK_SCHEDULER]
        The function to create TaskScheduler.

    Returns
    -------
    lib : ExecutorFactoryModule
        The built runtime module for the given relay workload.
    """

    logger.info("Working directory: %s", work_dir)
    extracted_tasks = extract_task_from_relay(mod, target, params)
    # pylint: disable=protected-access
    tune_contexts = []
    target = Parse._target(target)
    database = Parse._database(database, task_name, work_dir)
    # parse the tuning contexts
    for task in extracted_tasks:
        assert len(task.dispatched) == 1, "Only size 1 dispatched task list is supported for now"
        tune_contexts.append(
            Parse._tune_context(
                tune_context=None,
                mod=Parse._mod(task.dispatched[0]),
                target=target,
                config=config,
                task_name=task.task_name,
                space_generator=space,
                sch_rules=sch_rules,
                postprocs=postprocs,
                mutator_probs=mutator_probs,
                num_threads=num_threads,
            )
        )
    # deduplication
    logger.info("Before task deduplication: %d tasks", len(tune_contexts))
    tasks: List[TuneContext] = []
    hashs: List[int] = []
    for i, task in enumerate(tune_contexts):
        struct_hash: int = structural_hash(task.mod)
        flag: bool = False
        if struct_hash in hashs:
            for other_task in tune_contexts[i + 1 :]:
                if structural_equal(task.mod, other_task.mod):
                    flag = True
                    break
        if not flag:
            tasks.append(task)
            hashs.append(struct_hash)
    logger.info("After task deduplication: %d tasks", len(tasks))

    # parse the task scheduler
    task_scheduler = Parse._task_scheduler(
        task_scheduler,
        tasks,
        builder=Parse._builder(builder),
        runner=Parse._runner(runner),
        database=database,
        cost_model=Parse._cost_model(cost_model),
        measure_callbacks=Parse._callbacks(measure_callbacks),
    )
    # pylint: enable=protected-access
    task_scheduler.tune()
    with ApplyHistoryBest(database):
        with tvm.transform.PassContext(
            opt_level=3,
            config={"relay.backend.use_meta_schedule": True},
        ):
            return relay.build(mod, target=target, params=params)
