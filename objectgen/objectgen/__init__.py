import attr
import io
from pathlib import Path
from typing import List, Optional
from collections import defaultdict

LICENSE = """
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
"""

Namespace = List[str]
Type = str

# TODO:
# fix header
# normalize ns handling
# generate .gitignore

@attr.s(auto_attribs=True)
class ObjectField:
    field_name: str
    field_type: Type

@attr.s
class ObjectMethod:
    pass

@attr.s(auto_attribs=True)
class ObjectDefinition:
    name: str
    fields: List[ObjectField]
    methods: List[ObjectMethod] = []
    inherits_from: str = "ObjectRef"
    namespace: Namespace = []
    imports: List[Namespace] = []
    final: bool = True
    docs: str = ""

    def ref_name(self):
        return self.name

    def payload_name(self):
        return self.name + "Node"

    def parent_payload_name(self):
        if self.inherits_from != "ObjectRef":
            return self.inherits_from + "Node"
        return "Object"

    def parent_ref_name(self):
        if self.inherits_from != "ObjectRef":
            return self.inherits_from
        return "ObjectRef"

    def type_key(self):
        return ".".join(self.namespace + [self.name])

    def ctor_pf(self):
        # this is a temporary hack, need to think about how to clean up ns handling
        return ".".join(self.namespace[:-1] + [self.name])

@attr.s(auto_attribs=True)
class ObjectGenConfig:
    cpp_include_root: Optional[Path]
    cpp_source_root: Optional[Path]
    python_root: Optional[Path]
    root_namespace: List[str]

@attr.s(auto_attribs=True)
class Generator:
    config: ObjectGenConfig
    generated_files: List[Path] = attr.Factory(list)

    def qualified_path(self, defn):
        ns = self.config.root_namespace + defn.namespace
        return tuple(ns)

    def open_file(self, file_name, mode='w'):
        self.generated_files.append(file_name)
        return open(file_name, mode)

    def generate_gitignore(self, ns):
        raise NotImplementedError()


class CPPGenerator(Generator):
    def header_for(self, ns):
        ns = ns_to_path(ns)
        path = Path(self.config.cpp_include_root.joinpath(ns)).resolve()
        path.parents[0].mkdir(parents=True, exist_ok=True)
        return path.with_suffix(".h")

    def source_for(self, ns):
        ns = ns_to_path(ns)
        path = Path(self.config.cpp_source_root.joinpath(ns)).resolve()
        path.parents[0].mkdir(parents=True, exist_ok=True)
        return path.with_suffix(".cc")

    def generate_gitignore(self, ns):
        # TODO(@jroesch): unify with above code
        ns = ns_to_path(ns)
        source_path = path = Path(self.config.cpp_source_root.joinpath(ns)).resolve()
        source_path.parents[0].mkdir(parents=True, exist_ok=True)
        source_path = source_path.parents[0]

        header_path = Path(self.config.cpp_include_root.joinpath(ns)).resolve()
        header_path.parents[0].mkdir(parents=True, exist_ok=True)
        header_path = header_path.parents[0]

        header_ignore = header_path.joinpath(".gitignore")
        source_ignore = source_path.joinpath(".gitignore")

        with open(header_ignore, 'w') as header_ignore:
            with open(source_ignore, 'w') as source_ignore:
                for file_name in self.generated_files:
                    if file_name.suffix == ".h":
                        file_to_ignore = file_name.relative_to(header_path)
                        header_ignore.write(f"{file_to_ignore}\n")
                    elif file_name.suffix == ".cc":
                        file_to_ignore = file_name.relative_to(source_path)
                        source_ignore.write(f"{file_to_ignore}\n")

    def generate(self, definitions):
        by_ns = defaultdict(list)

        # Group definitions by namespaces.
        for defn in definitions:
            ns = self.qualified_path(defn)
            by_ns[ns].append(defn)

        # Generate each NS to a set of files.
        for ns in by_ns:
            header = io.StringIO("")
            source = io.StringIO("")

            self.generate_ns(header, source, ns, by_ns[ns])

            # Ensure directory exists.
            header_file = self.header_for(ns)
            source_file = self.source_for(ns)
            print(f"HeaderFile: {header_file}")
            print(f"SourceFile: {source_file}")

            license_str =("\n").join([f"* {line}" for line in LICENSE.splitlines()])
            license_str = f"/{license_str}\n*/"

            with self.open_file(header_file) as file:
                file.seek(0)
                file.truncate()
                file.write(license_str)
                file.write(header.getvalue())

            with self.open_file(source_file) as file:
                file.seek(0)
                file.truncate()
                file.write(license_str)
                file.write("\n")
                file.write(source.getvalue())

            self.generate_gitignore(ns)


    def generate_ns(self, header_buf, source_buf, namespace, defs):
        header_value = "_".join([ns.upper() for ns in namespace])
        header_value = f"TVM_{header_value}_H_"
        header_buf.write("\n")
        header_buf.write(f"#ifndef {header_value}\n")
        header_buf.write(f"#define {header_value}\n")

        includes = []

        for defn in defs:
            for imp in defn.imports:
                header_path = "/".join(imp) + ".h"
                includes += [f"<{header_path}>"]

        includes += [f"\"{self.header_for(namespace)}\""]

        import pdb; pdb.set_trace()

        source_buf.write("\n")
        header_buf.write("\n")
        for include in includes:
            source_buf.write(f"#include {include}\n")
            header_buf.write(f"#include {include}\n")
        source_buf.write("\n")
        header_buf.write("\n")

        for ns in ["tvm"] + list(namespace):
            header_buf.write(f"namespace {ns} {{ \n")
            source_buf.write(f"namespace {ns} {{ \n")

        header_buf.write("\n")
        source_buf.write("\n")

        for defn in defs:
            self.generate_object_def(header_buf, source_buf, defn)

        for ns in reversed(["tvm"] + list(namespace)):
            header_buf.write(f"}} // namespace {ns} \n")
            source_buf.write(f"}} // namespace {ns} \n")

        header_buf.write(f"#endif  // {header_value}\n")

    def generate_object_def(self, header_buf, source_buf, object_def):
        header_buf.write(f"class {object_def.name};\n")

        if object_def.docs:
            header_buf.write(object_def.docs)
            header_buf.write("\n")

        self.generate_payload_decl(header_buf, object_def)
        self.generate_ref_decl(header_buf, object_def)
        self.generate_impl(source_buf, object_def)

    def generate_payload_decl(self, header_buf, object_def):
        ref = object_def.ref_name()
        payload = object_def.payload_name()
        parent_ref = object_def.parent_ref_name()
        parent_payload = object_def.parent_payload_name()

        header_buf.write(f"class {payload} : public {parent_payload} {{\n")
        header_buf.write(" public:\n")

        for field in object_def.fields:
            header_buf.write(f"{4 * ' '}{field.field_type} {field.field_name};\n")

        header_buf.write(f"{4 * ' '}void VisitAttrs(AttrVisitor* v) {{\n")
        for field in object_def.fields:
            header_buf.write(f"{8 * ' '}v->Visit(\"{field.field_name}\", &{field.field_name});\n")
        header_buf.write(f"{4 * ' '}}}\n")

        self.generate_equal_and_hash(header_buf, object_def)

        header_buf.write(f"{4 * ' '}static constexpr const char* _type_key = \"{object_def.type_key()}\";\n")
        header_buf.write(f"{4 * ' '}static constexpr const bool _type_has_method_sequal_reduce = true;\n")
        header_buf.write(f"{4 * ' '}static constexpr const bool _type_has_method_shash_reduce = true;\n")

        if object_def.final:
            macro_name = "TVM_DECLARE_FINAL_OBJECT_INFO"
        else:
            macro_name = "TVM_DECLARE_BASE_OBJECT_INFO"

        header_buf.write(f"{4 * ' '}{macro_name}({object_def.payload_name()}, {parent_payload});\n")

        header_buf.write("};\n\n")

    def generate_equal_and_hash(self, header_buf, object_def):
        # Equality
        header_buf.write(f"{4 * ' '}bool SEqualReduce(const {object_def.payload_name()}* other, SEqualReducer equal) const {{\n")

        header_buf.write(f"{8 * ' '}return")
        if len(object_def.fields):
            for i, field in enumerate(object_def.fields):
                header_buf.write(f" equal({field.field_name}, other->{field.field_name})")
                if i != len(object_def.fields) - 1:
                    header_buf.write(" && ")
        else:
            header_buf.write(" true")

        header_buf.write(";\n")
        header_buf.write(f"{4 * ' '}}}\n")

        # Hashing
        header_buf.write(f"{4 * ' '}void SHashReduce(SHashReducer hash_reduce) const {{\n")
        for field in object_def.fields:
            header_buf.write(f"{8 * ' '}hash_reduce({field.field_name});\n")
        header_buf.write(f"{4 * ' '}}}\n")

    def generate_ref_decl(self, header_buf, object_def):
        ref = object_def.ref_name()
        payload = object_def.payload_name()
        parent_ref = object_def.parent_ref_name()
        parent_payload = object_def.parent_payload_name()

        header_buf.write(f"class {ref} : public {parent_ref} {{\n")
        header_buf.write(" public:\n")

        if len(object_def.fields):
            self.generate_ctor_decl(header_buf, object_def)

        # TODO(@jroesch): ast nodes should be non-nullable need to fix default ctor issue
        # header_buf.write(f"{4 * ' '}TVM_DEFINE_NOTNULLABLE_OBJECT_REF_METHODS")
        header_buf.write(f"{4 * ' '}TVM_DEFINE_OBJECT_REF_METHODS")

        header_buf.write(f"({ref}, {parent_ref}, {payload});\n")

        header_buf.write("};\n\n")

    def generate_ctor_decl(self, header_buf, object_def):
        ref = object_def.ref_name()
        header_buf.write(f"{4 * ' '}TVM_DLL {ref}(\n")
        for i, field in enumerate(object_def.fields):
            header_buf.write(f"{8 * ' '}{field.field_type} {field.field_name}")
            if i != len(object_def.fields) - 1:
                header_buf.write(f",\n")
        header_buf.write(f"{4 * ' '});\n")

    def generate_impl(self, source_buf, object_def):
        ref = object_def.ref_name()
        payload = object_def.payload_name()
        parent_ref = object_def.parent_ref_name()
        parent_payload = object_def.parent_payload_name()

        if len(object_def.fields):
            self.generate_ctor_impl(source_buf, object_def)

        source_buf.write(f"TVM_REGISTER_NODE_TYPE({payload});\n\n")

        source_buf.write(f"TVM_REGISTER_GLOBAL(\"{object_def.ctor_pf()}\")")
        source_buf.write(f".set_body_typed([]")

        source_buf.write(f"(")
        for i, field in enumerate(object_def.fields):
            source_buf.write(f"{field.field_type} {field.field_name}")
            if i != len(object_def.fields) - 1:
                source_buf.write(f",")
        source_buf.write(f") {{\n")
        source_buf.write(f"{4 * ' '}return {ref}(")

        for i, field in enumerate(object_def.fields):
            source_buf.write(f"{field.field_name}")
            if i != len(object_def.fields) - 1:
                source_buf.write(f",")
        source_buf.write(");\n")
        source_buf.write(f"}});\n\n")

# TVM_STATIC_IR_FUNCTOR(ReprPrinter, vtable)
#     .set_dispatch<TupleNode>([](const ObjectRef& ref, ReprPrinter* p) {
#       auto* node = static_cast<const TupleNode*>(ref.get());
#       p->stream << "Tuple(" << node->fields << ")";
#     });
    def generate_ctor_impl(self, source_buf, object_def):
        ref = object_def.ref_name()
        payload = object_def.payload_name()

        source_buf.write(f"{ref}::{ref}(\n")
        for i, field in enumerate(object_def.fields):
            source_buf.write(f"{4 * ' '}{field.field_type} {field.field_name}")
            if i != len(object_def.fields) - 1:
                source_buf.write(f",\n")
        source_buf.write(f") {{\n")

        source_buf.write(f"{4 * ' '}ObjectPtr<{payload}> n = make_object<{payload}>();\n")

        for field in object_def.fields:
            name = field.field_name
            source_buf.write(f"{4 * ' '}n->{name} = std::move({name});\n")
        source_buf.write(f"{4 * ' '}data_ = std::move(n);\n")
        source_buf.write("}\n\n")


class PythonGenerator(Generator):
    def source_for(self, ns):
        ns = ns_to_path(ns)
        path = Path(self.config.python_root.joinpath(ns)).resolve()
        path.parents[0].mkdir(parents=True, exist_ok=True)
        return path.with_suffix(".py")

    def ffi_for(self, ns):
        ns = ns_to_path(ns)
        path = Path(self.config.python_root.joinpath(ns)).resolve()
        path.parents[0].mkdir(parents=True, exist_ok=True)
        return path.with_suffix(".py")

    def generate(self, definitions):
        by_ns = defaultdict(list)

        # Group definitions by namespaces.
        for defn in definitions:
            ns = self.qualified_path(defn)
            by_ns[ns].append(defn)

        for ns in by_ns:
            ns = ns[:-1]
            ffi_file = self.ffi_for(list(ns) + ["_ffi_api.py"])
            api_ns = ".".join(ns)
            license_str =("\n").join([f"# {line}" for line in LICENSE.splitlines()])

            with open(ffi_file, 'w') as file:
                print(f"FFI File: {ffi_file}")
                file.seek(0)
                file.truncate()
                file.write(license_str)
                file.write("\nfrom tvm import _ffi\n")
                file.write(f"_ffi._init_api(\"{api_ns}\", __name__)\n")
                file.write("\n")

        # Generate each NS to a set of files.
        for ns in by_ns:
            source = io.StringIO("")

            self.generate_ns(source, ns, by_ns[ns])

            # Ensure directory exists.
            source_file = self.source_for(ns)
            print(f"SourceFile: {source_file}")

            license_str =("\n").join([f"# {line}" for line in LICENSE.splitlines()])

            with open(source_file, 'w') as file:
                file.seek(0)
                file.truncate()
                file.write(license_str)
                file.write("\n")
                file.write("import tvm._ffi\n")
                file.write("from ..ir.base import Node\n")
                file.write("from . import _ffi_api\n")
                file.write("\n")
                file.write("ObjectRef = Node\n")
                file.write(source.getvalue())

    def generate_ns(self, source_buf, namespace, defs):
        for defn in defs:
            source_buf.write(f"@tvm._ffi.register_object(\"{defn.type_key()}\")\n")
            source_buf.write(f"class {defn.ref_name()}({defn.parent_ref_name()}):\n")
            source_buf.write(f"{4 * ' '}def __init__(self, ")

            for i, field in enumerate(defn.fields):
                source_buf.write(f"{field.field_name}")
                if i != len(defn.fields) - 1:
                    source_buf.write(", ")

            source_buf.write("):\n")
            source_buf.write(f"{8 * ' '}self.__init_handle_by_constructor__(_ffi_api.{defn.ref_name()}, ")

            for i, field in enumerate(defn.fields):
                source_buf.write(f" {field.field_name}")
                if i != len(defn.fields) - 1:
                    source_buf.write(", ")

            source_buf.write(")\n")
            source_buf.write("\n\n")

        self.generate_gitignore(namespace)

    def generate_gitignore(self, ns):
        # TODO(@jroesch): unify with above code
        ns = ns_to_path(ns)
        source_path = Path(self.config.python_root.joinpath(ns)).resolve()
        source_path.parents[0].mkdir(parents=True, exist_ok=True)
        source_path = source_path.parents[0]

        source_ignore = source_path.joinpath(".gitignore")

        with open(source_ignore, 'w') as source_ignore:
            for file_name in self.generated_files:
                file_to_ignore = file_name.relative_to(source_path)
                source_ignore.write(f"{file_to_ignore}\n")


def ns_to_path(ns):
    return "/".join(ns)

def resolve_parent_fields(definitions):
    parent_map = {}
    for defn in definitions:
        parent_map[defn.name] = defn

    for defn in definitions:
        if defn.inherits_from != "ObjectRef":
            parent_fields = parent_map[defn.inherits_from].fields
            defn.fields = defn.fields + parent_fields

def from_python(config, definitions):
    resolve_parent_fields(definitions)

    if config.cpp_include_root and config.cpp_source_root:
        cpp_gen = CPPGenerator(config)
        cpp_gen.generate(definitions)

    if config.python_root:
        py_gen = PythonGenerator(config)
        py_gen.generate(definitions)

def in_ns(ns, imports, defs):
    for defn in defs:
        defn.namespace = ns + defn.namespace
        defn.imports = imports + defn.imports

    return defs
