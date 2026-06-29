#!/usr/bin/env python3
"""
Generate libflatpak bindings from GObject Introspection (GIR) file.

This script parses Flatpak-1.0.gir and generates:
1. C++ N-API wrapper functions in src/flatpak.cc
2. JavaScript wrapper classes in index.js
3. TypeScript definitions in index.d.ts
"""

import argparse
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# -----------------------------------------------------------------------------
# Type mappings
# -----------------------------------------------------------------------------

GIR_TO_CPP_TYPES = {
    "gboolean": "bool",
    "gint": "int",
    "guint": "unsigned int",
    "gint8": "int8_t",
    "guint8": "uint8_t",
    "gint16": "int16_t",
    "guint16": "uint16_t",
    "gint32": "int32_t",
    "guint32": "uint32_t",
    "gint64": "int64_t",
    "guint64": "uint64_t",
    "glong": "long",
    "gulong": "unsigned long",
    "gshort": "short",
    "gushort": "unsigned short",
    "gsize": "size_t",
    "gssize": "ssize_t",
    "gdouble": "double",
    "gfloat": "float",
    "utf8": "const char*",
    "filename": "const char*",
    "gpointer": "void*",
    "none": "void",
    "GLib.Quark": "GQuark",
    "GLib.Bytes": "GBytes*",
    "GLib.HashTable": "GHashTable*",
    "GLib.KeyFile": "GKeyFile*",
    "GLib.Variant": "GVariant*",
    "GLib.List": "GList*",
    "GLib.PtrArray": "GPtrArray*",
    "GLib.Strv": "char**",
}

GIR_TO_JS_TYPES = {
    "gboolean": "boolean",
    "gint": "number",
    "guint": "number",
    "gint8": "number",
    "guint8": "number",
    "gint16": "number",
    "guint16": "number",
    "gint32": "number",
    "guint32": "number",
    "gint64": "number",
    "guint64": "number",
    "glong": "number",
    "gulong": "number",
    "gshort": "number",
    "gushort": "number",
    "gsize": "number",
    "gssize": "number",
    "gdouble": "number",
    "gfloat": "number",
    "utf8": "string",
    "filename": "string",
    "gpointer": "External",
    "none": "void",
    "GLib.Quark": "number",
    "GLib.Bytes": "External",
    "GLib.HashTable": "External",
    "GLib.KeyFile": "External",
    "GLib.Variant": "External",
    "GLib.List": "External",
    "GLib.PtrArray": "External",
    "GLib.Strv": "External",
}

CPP_TO_NAPI_TYPES = {
    "bool": "Boolean",
    "int": "Number",
    "unsigned int": "Number",
    "int8_t": "Number",
    "uint8_t": "Number",
    "int16_t": "Number",
    "uint16_t": "Number",
    "int32_t": "Number",
    "uint32_t": "Number",
    "int64_t": "Number",
    "uint64_t": "Number",
    "long": "Number",
    "unsigned long": "Number",
    "short": "Number",
    "unsigned short": "Number",
    "size_t": "Number",
    "ssize_t": "Number",
    "double": "Number",
    "float": "Number",
    "const char*": "String",
    "char*": "String",
    "void": "Undefined",
}

# -----------------------------------------------------------------------------
# Data structures
# -----------------------------------------------------------------------------


@dataclass
class Parameter:
    name: str
    gir_type: str
    c_type: str
    js_type: str
    transfer: str = "none"
    nullable: bool = False
    direction: str = "in"
    is_instance: bool = False
    caller_allocates: bool = False

    def is_pointer(self) -> bool:
        return "*" in self.c_type

    def is_gobject(self) -> bool:
        # Exclude enum types from being treated as GObjects
        if self.is_enum():
            return False
        return (
            self.gir_type.startswith("Flatpak.")
            or "Flatpak" in self.c_type
            or self.gir_type
            in [
                "Gio.File",
                "Gio.Cancellable",
                "Gio.FileMonitor",
                "GLib.Bytes",
                "GLib.HashTable",
                "GLib.KeyFile",
                "GLib.Variant",
                "GLib.List",
                "GLib.PtrArray",
                "GLib.Strv",
            ]
        )

    def is_enum(self) -> bool:
        # Check if this is an enum type
        # Check gir_type first
        if (
            self.gir_type.endswith("Type")
            or self.gir_type.endswith("Flags")
            or self.gir_type.endswith("Kind")
        ):
            return True

        # Check c_type for Flatpak enums
        if "Flatpak" in self.c_type:
            c_type_lower = self.c_type.lower()
            if (
                c_type_lower.endswith("kind")
                or c_type_lower.endswith("type")
                or c_type_lower.endswith("flags")
            ):
                return True

        return False

    def is_error_param(self) -> bool:
        return self.name == "error" and self.gir_type == "GLib.Error"


@dataclass
class ReturnValue:
    gir_type: str
    c_type: str
    js_type: str
    transfer: str = "none"
    nullable: bool = False
    element_type: str = ""

    def is_pointer(self) -> bool:
        return "*" in self.c_type

    def is_gobject(self) -> bool:
        # Exclude enum types from being treated as GObjects
        if self.is_enum():
            return False
        return (
            self.gir_type.startswith("Flatpak.")
            or "Flatpak" in self.c_type
            or self.gir_type
            in [
                "Gio.File",
                "Gio.Cancellable",
                "Gio.FileMonitor",
                "GLib.Bytes",
                "GLib.HashTable",
                "GLib.KeyFile",
                "GLib.Variant",
                "GLib.List",
                "GLib.PtrArray",
                "GLib.Strv",
            ]
        )

    def is_enum(self) -> bool:
        # Check if this is an enum type
        # Check gir_type first
        if (
            self.gir_type.endswith("Type")
            or self.gir_type.endswith("Flags")
            or self.gir_type.endswith("Kind")
        ):
            return True

        # Check c_type for Flatpak enums
        if "Flatpak" in self.c_type:
            c_type_lower = self.c_type.lower()
            if (
                c_type_lower.endswith("kind")
                or c_type_lower.endswith("type")
                or c_type_lower.endswith("flags")
            ):
                return True

        return False


@dataclass
class Function:
    name: str
    c_name: str
    parameters: List[Parameter]
    return_value: ReturnValue
    is_method: bool = False
    is_constructor: bool = False
    is_static: bool = False
    throws: bool = False

    def has_error_param(self) -> bool:
        return any(p.is_error_param() for p in self.parameters)

    def js_name(self) -> str:
        if self.is_constructor:
            return "new"
        # First handle hyphenated names
        if "-" in self.name:
            # Convert hyphenated-name to camelCase
            parts = self.name.split("-")
            if self.is_method and parts[0] in ["get", "set", "is"]:
                # Keep get/set/is prefix
                return parts[0] + "".join(p.capitalize() for p in parts[1:] if p)
            elif self.is_method:
                return parts[0] + "".join(p.capitalize() for p in parts[1:] if p)
            else:
                # Standalone function
                return parts[0] + "".join(p.capitalize() for p in parts[1:] if p)
        # Convert snake_case to camelCase
        parts = self.name.split("_")
        if self.is_method and parts[0] in ["get", "set", "is"]:
            # Keep get/set/is prefix
            return parts[0] + "".join(p.capitalize() for p in parts[1:])
        elif self.is_method:
            return parts[0] + "".join(p.capitalize() for p in parts[1:])
        else:
            # Standalone function
            return parts[0] + "".join(p.capitalize() for p in parts[1:])


@dataclass
class Property:
    name: str
    gir_type: str
    c_type: str
    js_type: str
    readable: bool = True
    writable: bool = False
    construct: bool = False

    def getter_name(self) -> str:
        if self.name.startswith("is_"):
            return self.name
        # Handle hyphenated property names
        if "-" in self.name:
            return f"get_{self.name.replace('-', '_')}"
        return f"get_{self.name}"

    def setter_name(self) -> str:
        # Handle hyphenated property names
        if "-" in self.name:
            return f"set_{self.name.replace('-', '_')}"
        return f"set_{self.name}"


@dataclass
class Class:
    name: str
    c_name: str
    parent: Optional[str] = None
    functions: List[Function] = field(default_factory=list)
    properties: List[Property] = field(default_factory=list)


@dataclass
class Namespace:
    name: str
    classes: List[Class] = field(default_factory=list)
    functions: List[Function] = field(default_factory=list)


class GIRParser:
    def __init__(self, gir_file: str):
        self.gir_file = gir_file
        self.tree = ET.parse(gir_file)
        self.root = self.tree.getroot()
        self.ns = {
            "gi": "http://www.gtk.org/introspection/core/1.0",
            "c": "http://www.gtk.org/introspection/c/1.0",
            "glib": "http://www.gtk.org/introspection/glib/1.0",
        }

    def parse(self) -> Namespace:
        """Parse the entire GIR file"""
        namespace = Namespace(name="Flatpak")
        seen_c_names = set()

        # Find all classes
        for class_elem in self.root.findall(".//gi:class", self.ns):
            cls = self.parse_class(class_elem)
            if cls:
                namespace.classes.append(cls)

        # Find all standalone functions
        for func_elem in self.root.findall(".//gi:function", self.ns):
            func = self.parse_function(func_elem)
            if func and func.c_name not in seen_c_names:
                namespace.functions.append(func)
                seen_c_names.add(func.c_name)

        return namespace

    def parse_class(self, class_elem) -> Optional[Class]:
        """Parse a class element"""
        name = class_elem.get("name")
        if not name:
            return None

        c_name = class_elem.get(f"{{{self.ns['c']}}}type")
        if not c_name:
            c_name = f"Flatpak{name}"

        parent = class_elem.get("parent")
        # Normalize parent class name by stripping namespace prefix
        if parent:
            # Extract simple class name after last dot
            if "." in parent:
                parent = parent.split(".")[-1]

        cls = Class(name=name, c_name=c_name, parent=parent)

        # Parse constructors
        for constr_elem in class_elem.findall(".//gi:constructor", self.ns):
            func = self.parse_function(constr_elem, is_constructor=True)
            if func:
                cls.functions.append(func)

        # Parse methods
        for method_elem in class_elem.findall(".//gi:method", self.ns):
            func = self.parse_function(method_elem, is_method=True)
            if func:
                cls.functions.append(func)

        # Parse static methods
        for static_elem in class_elem.findall(".//gi:static-method", self.ns):
            func = self.parse_function(static_elem, is_method=True, is_static=True)
            if func:
                cls.functions.append(func)

        # Parse properties
        for prop_elem in class_elem.findall(".//gi:property", self.ns):
            prop = self.parse_property(prop_elem)
            if prop:
                cls.properties.append(prop)

        return cls

    def parse_function(
        self,
        func_elem,
        is_method: bool = False,
        is_constructor: bool = False,
        is_static: bool = False,
    ) -> Optional[Function]:
        """Parse a function element"""
        name = func_elem.get("name")
        if not name:
            return None

        c_name = func_elem.get(f"{{{self.ns['c']}}}identifier")
        if not c_name:
            c_name = name

        # Check if function throws errors
        throws = func_elem.get("throws", "0") == "1"

        # Parse parameters
        parameters = []
        has_callback = False
        has_array_param = False
        # Look for parameters under gi:parameters container
        params_container = func_elem.find("gi:parameters", self.ns)
        if params_container is not None:
            for param_elem in params_container.findall("gi:parameter", self.ns):
                # Check for array parameters
                if param_elem.find("gi:array", self.ns) is not None:
                    has_array_param = True
                param = self.parse_parameter(param_elem, is_instance=False)
                if param:
                    # Check for callback parameters
                    if (
                        "callback" in param.gir_type.lower()
                        or "Callback" in param.gir_type
                    ):
                        has_callback = True
                    parameters.append(param)
        else:
            # Fallback to searching all parameter elements
            for param_elem in func_elem.findall(".//gi:parameter", self.ns):
                # Check for array parameters
                if param_elem.find("gi:array", self.ns) is not None:
                    has_array_param = True
                param = self.parse_parameter(param_elem, is_instance=False)
                if param:
                    # Check for callback parameters
                    if (
                        "callback" in param.gir_type.lower()
                        or "Callback" in param.gir_type
                    ):
                        has_callback = True
                    parameters.append(param)

        # Parse return value
        return_elem = func_elem.find("gi:return-value", self.ns)
        if return_elem is not None:
            return_value = self.parse_return_value(return_elem)
        else:
            return_value = ReturnValue(
                gir_type="none", c_type="void", js_type="void", transfer="none"
            )

        # For methods (non-static), check for instance-parameter
        if is_method and not is_static:
            instance_param_elem = func_elem.find("gi:instance-parameter", self.ns)
            if instance_param_elem is not None:
                param = self.parse_parameter(instance_param_elem, is_instance=True)
                if param:
                    parameters.insert(0, param)

        # Skip functions with callback parameters (too complex for initial version)
        if has_callback:
            return None

        # Skip functions with array parameters (too complex for initial version)
        if has_array_param:
            return None

        return Function(
            name=name,
            c_name=c_name,
            parameters=parameters,
            return_value=return_value,
            is_method=is_method,
            is_constructor=is_constructor,
            is_static=is_static,
            throws=throws,
        )

    def parse_parameter(self, param_elem, is_instance=False) -> Optional[Parameter]:
        """Parse a parameter element"""
        name = param_elem.get("name", "")
        if not name:
            return None

        # Get type info
        type_elem = param_elem.find("gi:type", self.ns)
        if type_elem is None:
            return None

        gir_type = type_elem.get("name", "")
        c_type = type_elem.get(f"{{{self.ns['c']}}}type")
        if c_type is None:
            c_type = ""

        # Get attributes
        transfer = param_elem.get("transfer-ownership", "none")
        nullable = param_elem.get("nullable", "0") == "1"
        direction = param_elem.get("direction", "in")
        caller_allocates = param_elem.get("caller-allocates", "0") == "1"

        # Detect output parameters by name convention and type
        if direction == "in":
            # Check for common output parameter naming patterns
            if (
                name.endswith("_out")
                or name.endswith("_inout")
                or name.startswith("out_")
            ):
                direction = "out"
            # Check for pointer-to-pointer types (common for output parameters)
            elif c_type.count("*") == 2:  # e.g., FlatpakInstance**
                direction = "out"
            # Also check for pointer types with output naming patterns
            elif (
                name.endswith("_out")
                or name.endswith("_inout")
                or name.startswith("out_")
            ) and "*" in c_type:
                direction = "out"

        # Map to JS type
        js_type = self.map_gir_to_js_type(gir_type)

        return Parameter(
            name=name,
            gir_type=gir_type,
            c_type=c_type,
            js_type=js_type,
            transfer=transfer,
            nullable=nullable,
            direction=direction,
            is_instance=is_instance,
            caller_allocates=caller_allocates,
        )

    def parse_return_value(self, return_elem) -> ReturnValue:
        """Parse a return value element"""
        # Check for array type first
        array_elem = return_elem.find("gi:array", self.ns)
        if array_elem is not None:
            # Handle array return type
            c_type = array_elem.get(f"{{{self.ns['c']}}}type", "")
            array_name = array_elem.get("name", "")
            element_type = ""

            # Get the element type inside the array
            elem_type_elem = array_elem.find("gi:type", self.ns)
            if elem_type_elem is not None:
                element_type = elem_type_elem.get("name", "")

            # Check for known array types
            if array_name == "GLib.PtrArray" or "GPtrArray*" in c_type:
                gir_type = "GLib.PtrArray"
                if not c_type:
                    c_type = "GPtrArray*"
            elif array_name == "GLib.List":
                gir_type = "GLib.List"
                if not c_type:
                    c_type = "GList*"
            else:
                if elem_type_elem is not None:
                    gir_type = element_type
                    # For string arrays, use GLib.Strv
                    if gir_type == "utf8":
                        gir_type = "GLib.Strv"
                        if not c_type:
                            c_type = "char**"
                    else:
                        # Generic array type
                        gir_type = f"{gir_type}[]"
                else:
                    gir_type = "unknown[]"

            transfer = return_elem.get("transfer-ownership", "none")
            nullable = return_elem.get("nullable", "0") == "1"
            js_type = self.map_gir_to_js_type(gir_type)

            return ReturnValue(
                gir_type=gir_type,
                c_type=c_type,
                js_type=js_type,
                transfer=transfer,
                nullable=nullable,
                element_type=element_type,
            )

        # Check for regular type
        type_elem = return_elem.find("gi:type", self.ns)
        if type_elem is None:
            # Default to void
            return ReturnValue(
                gir_type="none", c_type="void", js_type="void", transfer="none"
            )

        gir_type = type_elem.get("name", "none")
        c_type = type_elem.get(f"{{{self.ns['c']}}}type")
        if c_type is None:
            # If c_type is not provided, try to map from gir_type
            if gir_type == "none":
                c_type = "void"
            elif gir_type in GIR_TO_CPP_TYPES:
                c_type = GIR_TO_CPP_TYPES[gir_type]
            elif gir_type == "GLib.Strv":
                c_type = "char**"
            else:
                c_type = "void"

        transfer = return_elem.get("transfer-ownership", "none")
        nullable = return_elem.get("nullable", "0") == "1"

        js_type = self.map_gir_to_js_type(gir_type)

        return ReturnValue(
            gir_type=gir_type,
            c_type=c_type,
            js_type=js_type,
            transfer=transfer,
            nullable=nullable,
            element_type="",
        )

    def parse_property(self, prop_elem) -> Optional[Property]:
        """Parse a property element"""
        name = prop_elem.get("name")
        if not name:
            return None

        # Get type info
        type_elem = prop_elem.find("gi:type", self.ns)
        if type_elem is None:
            return None

        gir_type = type_elem.get("name", "")
        c_type = type_elem.get(f"{{{self.ns['c']}}}type")
        if c_type is None:
            c_type = ""

        readable = prop_elem.get("readable", "1") == "1"
        writable = prop_elem.get("writable", "0") == "1"
        construct = prop_elem.get("construct", "0") == "1"

        js_type = self.map_gir_to_js_type(gir_type)

        return Property(
            name=name,
            gir_type=gir_type,
            c_type=c_type,
            js_type=js_type,
            readable=readable,
            writable=writable,
            construct=construct,
        )

    def map_gir_to_js_type(self, gir_type: str) -> str:
        """Map GIR type to JavaScript type"""
        # Check for Flatpak types
        if gir_type.startswith("Flatpak."):
            # Check for enum types (end with Type or Flags)
            if gir_type.endswith("Type") or gir_type.endswith("Flags"):
                return "number"
            return "External"

        # Check for Flatpak enum types without Flatpak. prefix (e.g., RefKind)
        if (
            gir_type.endswith("Kind")
            or gir_type.endswith("Type")
            or gir_type.endswith("Flags")
        ):
            return "number"

        # Check for array types
        if gir_type == "GLib.PtrArray":
            return "Array"

        # Check for GObject types
        if gir_type in [
            "Gio.File",
            "Gio.Cancellable",
            "Gio.FileMonitor",
            "GLib.Bytes",
            "GLib.HashTable",
            "GLib.KeyFile",
            "GLib.Variant",
            "GLib.List",
            "GLib.PtrArray",
            "GLib.Strv",
        ]:
            return "External"

        # Check for arrays
        if gir_type.endswith("[]"):
            return "Array"

        # Check for basic types
        if gir_type in GIR_TO_JS_TYPES:
            return GIR_TO_JS_TYPES[gir_type]

        # Default to any
        return "any"


class CppGenerator:
    def __init__(self, namespace: Namespace):
        self.namespace = namespace
        self.output = []

    def generate(self) -> str:
        """Generate C++ wrapper code"""
        self.output = []
        self.output.append("// Generated by generate_from_gir.py")
        self.output.append("// DO NOT EDIT THIS FILE DIRECTLY")
        self.output.append("")
        self.output.append("#include <flatpak/flatpak.h>")
        self.output.append("#include <glib.h>")
        self.output.append("#include <memory>")
        self.output.append("#include <napi.h>")
        self.output.append("#include <string>")
        self.output.append("#include <vector>")
        self.output.append("")

        self.generate_class_forward_decls()
        self.output.append("")
        self.generate_class_wrappers()
        self.output.append("")
        self.generate_init_function()

        return "\n".join(self.output)

    def generate_class_forward_decls(self):
        """Generate forward declarations for wrapper functions"""
        for cls in self.namespace.classes:
            for func in cls.functions:
                if func.is_constructor:
                    self.output.append(
                        f"Napi::Value Wrap_{cls.name}_{func.name}(const Napi::CallbackInfo& info);"
                    )
                elif func.is_static:
                    self.output.append(
                        f"Napi::Value Wrap_{cls.name}_{func.name}(const Napi::CallbackInfo& info);"
                    )
                else:
                    self.output.append(
                        f"Napi::Value Wrap_{cls.name}_{func.name}(const Napi::CallbackInfo& info);"
                    )

        for func in self.namespace.functions:
            self.output.append(
                f"Napi::Value Wrap_{func.c_name}(const Napi::CallbackInfo& info);"
            )

    def generate_class_wrappers(self):
        """Generate wrapper functions for each class"""
        for cls in self.namespace.classes:
            for func in cls.functions:
                if func.is_constructor:
                    self.generate_constructor_wrapper(cls, func)
                elif func.is_static:
                    self.generate_static_method_wrapper(cls, func)
                else:
                    self.generate_method_wrapper(cls, func)

        for func in self.namespace.functions:
            self.generate_function_wrapper(func)

    def generate_constructor_wrapper(self, cls: Class, func: Function):
        """Generate wrapper for a constructor"""
        self.output.append(
            f"Napi::Value Wrap_{cls.name}_{func.name}(const Napi::CallbackInfo& info) {{"
        )
        self.output.append("  Napi::Env env = info.Env();")
        self.output.append("")

        # Handle instance parameter (implicit 'this')
        cpp_params = []

        # Generate parameter extraction code
        # JavaScript parameters start at index 0 (no instance parameter for constructors)
        js_param_index = 0
        for param in func.parameters:
            if param.is_instance:
                # Skip instance parameter for constructors
                continue
            self.generate_parameter_code(param, js_param_index, cpp_params)
            js_param_index += 1

        # Handle error parameter
        error_param_name = None
        for param in func.parameters:
            if param.is_error_param():
                error_param_name = param.name
                break

        # If function throws but no error param found, add one
        if func.throws and not error_param_name:
            error_param_name = "error"

        if error_param_name:
            self.output.append(f"  GError* {error_param_name} = NULL;")

        # Generate function call
        if func.return_value.c_type == "void":
            call_line = f"  {func.c_name}("
            call_line += ", ".join(cpp_params)
            if error_param_name:
                if cpp_params:
                    call_line += f", &{error_param_name}"
                else:
                    call_line += f"&{error_param_name}"
            call_line += ");"
            self.output.append(call_line)
            self.output.append("")
            result_var = None
        else:
            result_var = "result"
            call_line = f"  {func.return_value.c_type} {result_var} = {func.c_name}("
            call_line += ", ".join(cpp_params)
            if error_param_name:
                if cpp_params:
                    call_line += f", &{error_param_name}"
                else:
                    call_line += f"&{error_param_name}"
            call_line += ");"
            self.output.append(call_line)
            self.output.append("")

        # Error handling
        if error_param_name:
            self.output.append(f"  if ({error_param_name}) {{")
            self.output.append(
                f"    Napi::Error::New(env, {error_param_name}->message).ThrowAsJavaScriptException();"
            )
            self.output.append(f"    g_error_free({error_param_name});")
            self.output.append("    return env.Null();")
            self.output.append("  }")
            self.output.append("")

        # Return conversion
        if result_var is not None:
            self.generate_return_conversion(func.return_value, result_var)
        else:
            self.generate_return_conversion(func.return_value, "")
        self.output.append("}")
        self.output.append("")

    def generate_method_wrapper(self, cls: Class, func: Function):
        """Generate wrapper for an instance method"""
        self.output.append(
            f"Napi::Value Wrap_{cls.name}_{func.name}(const Napi::CallbackInfo& info) {{"
        )
        self.output.append("  Napi::Env env = info.Env();")
        self.output.append("")

        # First parameter is the instance (this)
        self.output.append("  if (info.Length() < 1 || !info[0].IsExternal()) {")
        self.output.append(
            f'    Napi::TypeError::New(env, "Expected {cls.name} instance").ThrowAsJavaScriptException();'
        )
        self.output.append("    return env.Null();")
        self.output.append("  }")
        self.output.append(
            f"  {cls.c_name}* self = info[0].As<Napi::External<{cls.c_name}>>().Data();"
        )
        self.output.append("")
        self.output.append("  if (!self) {")
        self.output.append(
            f'    Napi::Error::New(env, "Invalid {cls.name} instance (null pointer)").ThrowAsJavaScriptException();'
        )
        self.output.append("    return env.Null();")
        self.output.append("  }")
        self.output.append("")

        # Generate parameter extraction code (skip first param for instance)
        cpp_params = ["self"]

        # JavaScript parameters start at index 1 (index 0 is the instance)
        js_param_index = 1
        for param in func.parameters:
            if param.is_instance:
                # Skip instance parameter
                continue
            self.generate_parameter_code(param, js_param_index, cpp_params)
            js_param_index += 1

        # Handle error parameter
        error_param_name = None
        for param in func.parameters:
            if param.is_error_param():
                error_param_name = param.name
                break

        # If function throws but no error param found, add one
        if func.throws and not error_param_name:
            error_param_name = "error"

        if error_param_name:
            self.output.append(f"  GError* {error_param_name} = NULL;")

        # Generate function call
        if func.return_value.c_type == "void":
            call_line = f"  {func.c_name}("
            call_line += ", ".join(cpp_params)
            if error_param_name:
                if cpp_params:
                    call_line += f", &{error_param_name}"
                else:
                    call_line += f"&{error_param_name}"
            call_line += ");"
            self.output.append(call_line)
            self.output.append("")
            result_var = None
        else:
            result_var = "result"
            call_line = f"  {func.return_value.c_type} {result_var} = {func.c_name}("
            call_line += ", ".join(cpp_params)
            if error_param_name:
                if cpp_params:
                    call_line += f", &{error_param_name}"
                else:
                    call_line += f"&{error_param_name}"
            call_line += ");"
            self.output.append(call_line)
            self.output.append("")

        # Error handling
        if error_param_name:
            self.output.append(f"  if ({error_param_name}) {{")
            self.output.append(
                f"    Napi::Error::New(env, {error_param_name}->message).ThrowAsJavaScriptException();"
            )
            self.output.append(f"    g_error_free({error_param_name});")
            self.output.append("    return env.Null();")
            self.output.append("  }")
            self.output.append("")

        # Return conversion
        if result_var is not None:
            self.generate_return_conversion(func.return_value, result_var)
        else:
            self.generate_return_conversion(func.return_value, "")
        self.output.append("}")
        self.output.append("")

    def generate_static_method_wrapper(self, cls: Class, func: Function):
        """Generate wrapper for a static method"""
        self.output.append(
            f"Napi::Value Wrap_{cls.name}_{func.name}(const Napi::CallbackInfo& info) {{"
        )
        self.output.append("  Napi::Env env = info.Env();")
        self.output.append("")

        # Generate parameter extraction code
        cpp_params = []

        # JavaScript parameters start at index 0 for static methods
        for i, param in enumerate(func.parameters):
            self.generate_parameter_code(param, i, cpp_params)

        # Handle error parameter
        error_param_name = None
        for param in func.parameters:
            if param.is_error_param():
                error_param_name = param.name
                break

        # If function throws but no error param found, add one
        if func.throws and not error_param_name:
            error_param_name = "error"

        if error_param_name:
            self.output.append(f"  GError* {error_param_name} = NULL;")

        # Generate function call
        if func.return_value.c_type == "void":
            call_line = f"  {func.c_name}("
            call_line += ", ".join(cpp_params)
            if error_param_name:
                if cpp_params:
                    call_line += f", &{error_param_name}"
                else:
                    call_line += f"&{error_param_name}"
            call_line += ");"
            self.output.append(call_line)
            self.output.append("")
            result_var = None
        else:
            result_var = "result"
            call_line = f"  {func.return_value.c_type} {result_var} = {func.c_name}("
            call_line += ", ".join(cpp_params)
            if error_param_name:
                if cpp_params:
                    call_line += f", &{error_param_name}"
                else:
                    call_line += f"&{error_param_name}"
            call_line += ");"
            self.output.append(call_line)
            self.output.append("")

        # Error handling
        if error_param_name:
            self.output.append(f"  if ({error_param_name}) {{")
            self.output.append(
                f"    Napi::Error::New(env, {error_param_name}->message).ThrowAsJavaScriptException();"
            )
            self.output.append(f"    g_error_free({error_param_name});")
            self.output.append("    return env.Null();")
            self.output.append("  }")
            self.output.append("")

        # Return conversion
        if result_var is not None:
            self.generate_return_conversion(func.return_value, result_var)
        else:
            self.generate_return_conversion(func.return_value, "")
        self.output.append("}")
        self.output.append("")

    def generate_function_wrapper(self, func: Function):
        """Generate wrapper for a standalone function"""
        self.output.append(
            f"Napi::Value Wrap_{func.c_name}(const Napi::CallbackInfo& info) {{"
        )
        self.output.append("  Napi::Env env = info.Env();")
        self.output.append("")

        # Generate parameter extraction code
        cpp_params = []

        for i, param in enumerate(func.parameters):
            self.generate_parameter_code(param, i, cpp_params)

        # Handle error parameter
        error_param_name = None
        for param in func.parameters:
            if param.is_error_param():
                error_param_name = param.name
                break

        # If function throws but no error param found, add one
        if func.throws and not error_param_name:
            error_param_name = "error"

        if error_param_name:
            self.output.append(f"  GError* {error_param_name} = NULL;")

        # Generate function call
        if func.return_value.c_type == "void":
            call_line = f"  {func.c_name}("
            call_line += ", ".join(cpp_params)
            if error_param_name:
                if cpp_params:
                    call_line += f", &{error_param_name}"
                else:
                    call_line += f"&{error_param_name}"
            call_line += ");"
            self.output.append(call_line)
            self.output.append("")
            result_var = None
        else:
            result_var = "result"
            call_line = f"  {func.return_value.c_type} {result_var} = {func.c_name}("
            call_line += ", ".join(cpp_params)
            if error_param_name:
                if cpp_params:
                    call_line += f", &{error_param_name}"
                else:
                    call_line += f"&{error_param_name}"
            call_line += ");"
            self.output.append(call_line)
            self.output.append("")

        # Error handling
        if error_param_name:
            self.output.append(f"  if ({error_param_name}) {{")
            self.output.append(
                f"    Napi::Error::New(env, {error_param_name}->message).ThrowAsJavaScriptException();"
            )
            self.output.append(f"    g_error_free({error_param_name});")
            self.output.append("    return env.Null();")
            self.output.append("  }")
            self.output.append("")

        # Return conversion
        if result_var is not None:
            self.generate_return_conversion(func.return_value, result_var)
        else:
            self.generate_return_conversion(func.return_value, "")
        self.output.append("}")
        self.output.append("")

    def generate_parameter_code(
        self, param: Parameter, index: int, cpp_params: List[str]
    ):
        """Generate code to extract a parameter from JavaScript"""
        if param.is_error_param():
            # Skip error parameter - it's handled separately
            return

        if param.direction != "in":
            # Handle output parameters
            if param.direction == "out":
                # Create local variable for output parameter
                base_type = param.c_type.rstrip("*").strip()
                if param.is_gobject():
                    # GObject output parameter (pointer to pointer)
                    # Need to create FlatpakInstance* variable and pass &variable
                    # The C function expects FlatpakInstance** (address of pointer)
                    self.output.append(f"  {base_type}* {param.name}_local = NULL;")
                    self.output.append(
                        f"  {param.c_type} {param.name} = &{param.name}_local;"
                    )
                elif "Flatpak" in param.c_type and not param.is_pointer():
                    # Enum output parameter
                    self.output.append(f"  {param.c_type} {param.name}_local = 0;")
                    self.output.append(
                        f"  {param.c_type}* {param.name} = &{param.name}_local;"
                    )
                else:
                    # Other output parameter
                    self.output.append(f"  {base_type} {param.name}_local;")
                    self.output.append(
                        f"  {param.c_type} {param.name} = &{param.name}_local;"
                    )
                cpp_params.append(param.name)
            else:
                # inout or unknown direction
                cpp_params.append("NULL")
            return

        elif param.gir_type == "utf8" or param.gir_type == "filename":
            # Handle nullable string parameters
            if param.nullable:
                self.output.append(f"  const char* {param.name} = NULL;")
                self.output.append(
                    f"  if (info.Length() > {index} && !info[{index}].IsNull() && !info[{index}].IsUndefined()) {{"
                )
                self.output.append(f"    if (!info[{index}].IsString()) {{")
                self.output.append(
                    f"      Napi::TypeError::New(env, \"Expected string or null for parameter '{param.name}'\").ThrowAsJavaScriptException();"
                )
                self.output.append("      return env.Null();")
                self.output.append("    }")
                self.output.append(
                    f"    std::string {param.name}_str = info[{index}].As<Napi::String>().Utf8Value();"
                )
                self.output.append(f"    {param.name} = {param.name}_str.c_str();")
                self.output.append("  }")
            else:
                self.output.append(
                    f"  if (info.Length() <= {index} || !info[{index}].IsString()) {{"
                )
                self.output.append(
                    f"    Napi::TypeError::New(env, \"Expected string for parameter '{param.name}'\").ThrowAsJavaScriptException();"
                )
                self.output.append("    return env.Null();")
                self.output.append("  }")
                self.output.append(
                    f"  std::string {param.name}_str = info[{index}].As<Napi::String>().Utf8Value();"
                )
                self.output.append(
                    f"  const char* {param.name} = {param.name}_str.c_str();"
                )
            cpp_params.append(param.name)

        elif param.gir_type == "gboolean":
            self.output.append(
                f"  if (info.Length() <= {index} || !info[{index}].IsBoolean()) {{"
            )
            self.output.append(
                f"    Napi::TypeError::New(env, \"Expected boolean for parameter '{param.name}'\").ThrowAsJavaScriptException();"
            )
            self.output.append("    return env.Null();")
            self.output.append("  }")
            self.output.append(
                f"  gboolean {param.name} = info[{index}].As<Napi::Boolean>().Value();"
            )
            cpp_params.append(param.name)

        elif param.gir_type in [
            "gint",
            "guint",
            "gint8",
            "guint8",
            "gint16",
            "guint16",
            "gint32",
            "guint32",
            "gint64",
            "guint64",
            "glong",
            "gulong",
            "gshort",
            "gushort",
            "gsize",
            "gssize",
            "gdouble",
            "gfloat",
        ]:
            self.output.append(
                f"  if (info.Length() <= {index} || !info[{index}].IsNumber()) {{"
            )
            self.output.append(
                f"    Napi::TypeError::New(env, \"Expected number for parameter '{param.name}'\").ThrowAsJavaScriptException();"
            )
            self.output.append("    return env.Null();")
            self.output.append("  }")
            if "int" in param.gir_type or param.gir_type in [
                "glong",
                "gshort",
                "gsize",
                "gssize",
            ]:
                if (
                    "64" in param.gir_type
                    or param.gir_type == "gint64"
                    or param.gir_type == "guint64"
                ):
                    self.output.append(
                        f"  {param.c_type} {param.name} = info[{index}].As<Napi::Number>().Int64Value();"
                    )
                else:
                    self.output.append(
                        f"  {param.c_type} {param.name} = info[{index}].As<Napi::Number>().Int32Value();"
                    )
            else:
                self.output.append(
                    f"  {param.c_type} {param.name} = info[{index}].As<Napi::Number>().DoubleValue();"
                )
            cpp_params.append(param.name)

        # Check for enum types before GObject check
        elif param.is_enum():
            # Enum type - always treat as regular enum value for input parameters
            self.output.append(
                f"  if (info.Length() <= {index} || !info[{index}].IsNumber()) {{"
            )
            self.output.append(
                f"    Napi::TypeError::New(env, \"Expected number for enum parameter '{param.name}'\").ThrowAsJavaScriptException();"
            )
            self.output.append("    return env.Null();")
            self.output.append("  }")
            # Remove pointer if present in c_type (treat as regular enum)
            c_type_without_ptr = param.c_type.rstrip("*").strip()
            self.output.append(
                f"  {c_type_without_ptr} {param.name} = static_cast<{c_type_without_ptr}>(info[{index}].As<Napi::Number>().Int32Value());"
            )
            cpp_params.append(param.name)

        elif param.is_gobject():
            # Handle nullable GObject parameters
            if param.nullable:
                self.output.append(f"  {param.c_type} {param.name} = NULL;")
                self.output.append(
                    f"  if (info.Length() > {index} && !info[{index}].IsNull() && !info[{index}].IsUndefined()) {{"
                )
                self.output.append(f"    if (!info[{index}].IsExternal()) {{")
                self.output.append(
                    f"      Napi::TypeError::New(env, \"Expected external object or null for parameter '{param.name}'\").ThrowAsJavaScriptException();"
                )
                self.output.append("      return env.Null();")
                self.output.append("    }")
                # Extract the actual type from c_type (remove *)
                base_type = param.c_type.rstrip("*").strip()
                # Ensure base_type is a proper C type (not a GIR type)
                if base_type == "":
                    base_type = param.gir_type.split(".")[-1]
                    if base_type in [
                        "File",
                        "Cancellable",
                        "Bytes",
                        "HashTable",
                        "KeyFile",
                        "Variant",
                        "List",
                        "PtrArray",
                    ]:
                        base_type = "G" + base_type
                    else:
                        base_type = "Flatpak" + base_type
                self.output.append(
                    f"    {param.name} = info[{index}].As<Napi::External<{base_type}>>().Data();"
                )
                self.output.append("  }")
            else:
                self.output.append(
                    f"  if (info.Length() <= {index} || !info[{index}].IsExternal()) {{"
                )
                self.output.append(
                    f"    Napi::TypeError::New(env, \"Expected external object for parameter '{param.name}'\").ThrowAsJavaScriptException();"
                )
                self.output.append("    return env.Null();")
                self.output.append("  }")
                # Extract the actual type from c_type (remove *)
                base_type = param.c_type.rstrip("*").strip()
                # Ensure base_type is a proper C type (not a GIR type)
                if base_type == "":
                    base_type = param.gir_type.split(".")[-1]
                    if base_type in [
                        "File",
                        "Cancellable",
                        "Bytes",
                        "HashTable",
                        "KeyFile",
                        "Variant",
                        "List",
                        "PtrArray",
                    ]:
                        base_type = "G" + base_type
                    else:
                        base_type = "Flatpak" + base_type
                self.output.append(
                    f"  {param.c_type} {param.name} = info[{index}].As<Napi::External<{base_type}>>().Data();"
                )
            cpp_params.append(param.name)

        else:
            # Unknown type, pass as-is
            self.output.append(
                f"  // Parameter '{param.name}' of type '{param.gir_type}'"
            )
            self.output.append(f"  // TODO: Add proper conversion")
            cpp_params.append(f"/* {param.name}: {param.gir_type} */")

        self.output.append("")

    def generate_return_conversion(self, return_value: ReturnValue, var_name: str):
        """Generate code to convert return value to JavaScript"""
        if return_value.gir_type == "none":
            self.output.append("  return env.Undefined();")
            return

        elif return_value.gir_type == "utf8" or return_value.gir_type == "filename":
            if return_value.transfer in ["full", "container"]:
                self.output.append(
                    f'  Napi::String js_result = Napi::String::New(env, {var_name} ? {var_name} : "");'
                )
                self.output.append(f"  g_free({var_name});")
                self.output.append("  return js_result;")
            else:
                self.output.append(
                    f'  return Napi::String::New(env, {var_name} ? {var_name} : "");'
                )

        elif return_value.gir_type == "gboolean":
            self.output.append(f"  return Napi::Boolean::New(env, {var_name});")

        elif return_value.gir_type in [
            "gint",
            "guint",
            "gint8",
            "guint8",
            "gint16",
            "guint16",
            "gint32",
            "guint32",
            "gint64",
            "guint64",
            "glong",
            "gulong",
            "gshort",
            "gushort",
            "gsize",
            "gssize",
            "gdouble",
            "gfloat",
        ]:
            self.output.append(f"  return Napi::Number::New(env, {var_name});")

        elif return_value.gir_type == "GLib.Quark":
            self.output.append(f"  return Napi::Number::New(env, {var_name});")

        elif return_value.gir_type == "GLib.Strv":
            self.output.append(
                f"  // Convert string array (GLib.Strv) to JavaScript array"
            )
            self.output.append(f"  Napi::Array js_array = Napi::Array::New(env);")
            self.output.append(f"  if ({var_name}) {{")
            self.output.append(f"    int i = 0;")
            self.output.append(f"    while ({var_name}[i]) {{")
            self.output.append(
                f"      js_array.Set(i, Napi::String::New(env, {var_name}[i]));"
            )
            self.output.append(f"      i++;")
            self.output.append(f"    }}")
            self.output.append(f"  }}")
            if return_value.transfer in ["full", "container"]:
                self.output.append(f"  g_strfreev({var_name});")
            self.output.append(f"  return js_array;")

        elif return_value.gir_type == "GLib.PtrArray":
            self.output.append(f"  // Convert GPtrArray to JavaScript array")
            self.output.append(f"  Napi::Array js_array = Napi::Array::New(env);")
            self.output.append(f"  if ({var_name}) {{")
            self.output.append(f"    GPtrArray* array = {var_name};")
            self.output.append(f"    for (guint i = 0; i < array->len; i++) {{")
            self.output.append(f"      gpointer item = g_ptr_array_index(array, i);")
            self.output.append(f"      if (!item) {{")
            self.output.append(f"        js_array.Set(i, env.Null());")
            self.output.append(f"        continue;")
            self.output.append(f"      }}")
            # Determine element type and wrap appropriately
            if return_value.element_type:
                element_type = return_value.element_type
                # Map GIR type to C type
                if element_type in [
                    "InstalledRef",
                    "RemoteRef",
                    "Remote",
                    "Ref",
                    "RelatedRef",
                    "TransactionOperation",
                    "Instance",
                    "Installation",
                ]:
                    # These are Flatpak objects
                    c_type = f"Flatpak{element_type}*"
                    self.output.append(
                        f"      {c_type} typed_item = static_cast<{c_type}>(item);"
                    )
                    self.output.append(f"      if (!typed_item) {{")
                    self.output.append(f"        js_array.Set(i, env.Null());")
                    self.output.append(f"        continue;")
                    self.output.append(f"      }}")
                    self.output.append(
                        f"      // Increment reference count for GObject"
                    )
                    self.output.append(f"      if (G_IS_OBJECT(typed_item)) {{")
                    self.output.append(f"        g_object_ref(typed_item);")
                    self.output.append(f"        // Create external with finalizer")
                    self.output.append(
                        f"        js_array.Set(i, Napi::External<Flatpak{element_type}>::New(env, typed_item,"
                    )
                    self.output.append(
                        f"          [](Napi::Env env, Flatpak{element_type}* obj) {{"
                    )
                    self.output.append(f"            if (obj && G_IS_OBJECT(obj)) {{")
                    self.output.append(f"              g_object_unref(obj);")
                    self.output.append(f"            }}")
                    self.output.append(f"          }}));")
                    self.output.append(f"      }} else {{")
                    self.output.append(
                        f"        // Not a GObject, just pass as external"
                    )
                    self.output.append(
                        f"        js_array.Set(i, Napi::External<Flatpak{element_type}>::New(env, typed_item));"
                    )
                    self.output.append(f"      }}")
                elif element_type in [
                    "File",
                    "Cancellable",
                    "Bytes",
                    "HashTable",
                    "KeyFile",
                    "Variant",
                    "List",
                    "PtrArray",
                ]:
                    # GLib types
                    c_type = f"G{element_type}*"
                    self.output.append(
                        f"      {c_type} typed_item = static_cast<{c_type}>(item);"
                    )
                    self.output.append(f"      if (!typed_item) {{")
                    self.output.append(f"        js_array.Set(i, env.Null());")
                    self.output.append(f"        continue;")
                    self.output.append(f"      }}")
                    self.output.append(
                        f"      // Increment reference count for GObject"
                    )
                    self.output.append(f"      if (G_IS_OBJECT(typed_item)) {{")
                    self.output.append(f"        g_object_ref(typed_item);")
                    self.output.append(f"        // Create external with finalizer")
                    self.output.append(
                        f"        js_array.Set(i, Napi::External<G{element_type}>::New(env, typed_item,"
                    )
                    self.output.append(
                        f"          [](Napi::Env env, G{element_type}* obj) {{"
                    )
                    self.output.append(f"            if (obj && G_IS_OBJECT(obj)) {{")
                    self.output.append(f"              g_object_unref(obj);")
                    self.output.append(f"            }}")
                    self.output.append(f"          }}));")
                    self.output.append(f"      }} else {{")
                    self.output.append(
                        f"        // Not a GObject, just pass as external"
                    )
                    self.output.append(
                        f"        js_array.Set(i, Napi::External<G{element_type}>::New(env, typed_item));"
                    )
                    self.output.append(f"      }}")
                else:
                    # Unknown type, fallback to void*
                    self.output.append(f"      // Unknown element type: {element_type}")
                    self.output.append(f"      // Try to treat as GObject if possible")
                    self.output.append(
                        f"      GObject* gobj = static_cast<GObject*>(item);"
                    )
                    self.output.append(f"      if (gobj && G_IS_OBJECT(gobj)) {{")
                    self.output.append(f"        g_object_ref(gobj);")
                    self.output.append(f"        // Create external with finalizer")
                    self.output.append(
                        f"        js_array.Set(i, Napi::External<void>::New(env, gobj,"
                    )
                    self.output.append(f"          [](Napi::Env env, void* obj) {{")
                    self.output.append(f"            if (obj && G_IS_OBJECT(obj)) {{")
                    self.output.append(
                        f"              g_object_unref(static_cast<GObject*>(obj));"
                    )
                    self.output.append(f"            }}")
                    self.output.append(f"          }}));")
                    self.output.append(f"      }} else {{")
                    self.output.append(
                        f"        // Not a GObject, just pass as external"
                    )
                    self.output.append(
                        f"        js_array.Set(i, Napi::External<void>::New(env, item));"
                    )
                    self.output.append(f"      }}")
            else:
                # No element type info, try to treat as GObject if possible
                self.output.append(f"      // Try to treat as GObject")
                self.output.append(
                    f"      GObject* gobj = static_cast<GObject*>(item);"
                )
                self.output.append(f"      if (gobj && G_IS_OBJECT(gobj)) {{")
                self.output.append(f"        g_object_ref(gobj);")
                self.output.append(f"        // Create external with finalizer")
                self.output.append(
                    f"        js_array.Set(i, Napi::External<void>::New(env, gobj,"
                )
                self.output.append(f"          [](Napi::Env env, void* obj) {{")
                self.output.append(f"            if (obj && G_IS_OBJECT(obj)) {{")
                self.output.append(
                    f"              g_object_unref(static_cast<GObject*>(obj));"
                )
                self.output.append(f"            }}")
                self.output.append(f"          }}));")
                self.output.append(f"      }} else {{")
                self.output.append(f"        // Not a GObject, just pass as external")
                self.output.append(
                    f"        js_array.Set(i, Napi::External<void>::New(env, item));"
                )
                self.output.append(f"      }}")
            self.output.append(f"    }}")
            self.output.append(f"    // Unref the array but not the contained objects")
            if return_value.transfer in ["full", "container"]:
                self.output.append(f"    g_ptr_array_unref({var_name});")
            self.output.append(f"  }}")
            self.output.append(f"  return js_array;")

        elif return_value.is_gobject():
            if return_value.gir_type.startswith("Flatpak."):
                # Check if it's an enum type
                if (
                    return_value.gir_type.endswith("Type")
                    or return_value.gir_type.endswith("Flags")
                    or return_value.gir_type.endswith("Kind")
                ):
                    # Enum return type
                    self.output.append(
                        f"  return Napi::Number::New(env, static_cast<int32_t>({var_name}));"
                    )
                else:
                    # Regular Flatpak object
                    base_type = return_value.c_type.rstrip("*").strip()
                    # Ensure base_type is proper C type
                    base_type = return_value.c_type.rstrip("*").strip()
                    if base_type == "":
                        base_type = return_value.gir_type.split(".")[-1]
                        if base_type in [
                            "File",
                            "Cancellable",
                            "Bytes",
                            "HashTable",
                            "KeyFile",
                            "Variant",
                            "List",
                            "PtrArray",
                        ]:
                            base_type = "G" + base_type
                        else:
                            base_type = "Flatpak" + base_type
                    self.output.append(f"  if (!{var_name}) {{")
                    self.output.append(f"    return env.Null();")
                    self.output.append(f"  }}")
                    self.output.append(f"  // Increment reference count for GObject")
                    self.output.append(f"  if (G_IS_OBJECT({var_name})) {{")
                    self.output.append(f"    g_object_ref({var_name});")
                    self.output.append(f"    // Create external with finalizer")
                    self.output.append(
                        f"    return Napi::External<{base_type}>::New(env, {var_name},"
                    )
                    self.output.append(f"      [](Napi::Env env, {base_type}* obj) {{")
                    self.output.append(f"        if (obj && G_IS_OBJECT(obj)) {{")
                    self.output.append(f"          g_object_unref(obj);")
                    self.output.append(f"        }}")
                    self.output.append(f"      }});")
                    self.output.append(f"  }} else {{")
                    self.output.append(f"    // Not a GObject, just pass as external")
                    self.output.append(
                        f"    return Napi::External<{base_type}>::New(env, {var_name});"
                    )
                    self.output.append(f"  }}")
            else:
                self.output.append(
                    f"  // Return GObject of type {return_value.gir_type}"
                )
                self.output.append(f"  if (!{var_name}) {{")
                self.output.append(f"    return env.Null();")
                self.output.append(f"  }}")
                self.output.append(f"  // Increment reference count for GObject")
                self.output.append(f"  if (G_IS_OBJECT({var_name})) {{")
                self.output.append(f"    g_object_ref({var_name});")
                self.output.append(f"    // Create external with finalizer")
                self.output.append(
                    f"    return Napi::External<void>::New(env, {var_name},"
                )
                self.output.append(f"      [](Napi::Env env, void* obj) {{")
                self.output.append(f"        if (obj && G_IS_OBJECT(obj)) {{")
                self.output.append(
                    f"          g_object_unref(static_cast<GObject*>(obj));"
                )
                self.output.append(f"        }}")
                self.output.append(f"      }});")
                self.output.append(f"  }} else {{")
                self.output.append(f"    // Not a GObject, just pass as external")
                self.output.append(
                    f"    return Napi::External<void>::New(env, {var_name});"
                )
                self.output.append(f"  }}")

        elif return_value.is_enum():
            # Enum return type
            self.output.append(
                f"  return Napi::Number::New(env, static_cast<int32_t>({var_name}));"
            )

        elif return_value.gir_type == "GLib.Bytes":
            self.output.append(f"  // Convert GBytes to Buffer")
            self.output.append(f"  gsize buffer_size = 0;")
            self.output.append(
                f"  gconstpointer data = g_bytes_get_data({var_name}, &buffer_size);"
            )
            self.output.append(
                f"  Napi::Buffer<uint8_t> buffer = Napi::Buffer<uint8_t>::Copy(env, static_cast<const uint8_t*>(data), buffer_size);"
            )
            if return_value.transfer in ["full", "container"]:
                self.output.append(f"  g_bytes_unref({var_name});")
            self.output.append(f"  return buffer;")

        elif return_value.gir_type.endswith("[]"):
            self.output.append(f"  // Convert array of type {return_value.gir_type}")
            self.output.append(
                f"  // TODO: Implement array conversion for generic array type"
            )
            self.output.append(f"  return env.Null();")

        else:
            self.output.append(f"  // Unknown return type: {return_value.gir_type}")
            self.output.append(f"  return env.Null();")

    def generate_init_function(self):
        """Generate the N-API module initialization function"""
        self.output.append("Napi::Object Init(Napi::Env env, Napi::Object exports) {")

        # Export standalone functions with duplicate handling
        exported_names = set()
        for func in self.namespace.functions:
            js_name = func.js_name()
            # Unconditionally rename quark functions
            if "quark" in js_name:
                if func.c_name == "flatpak_error_quark":
                    js_name = "errorQuark"
                elif func.c_name == "flatpak_portal_error_quark":
                    js_name = "portalErrorQuark"
            # Handle duplicate function names (after quark renaming)
            if js_name in exported_names:
                # Add prefix to avoid duplicates
                js_name = f"{func.c_name.split('_')[0]}_{js_name}"
            exported_names.add(js_name)
            self.output.append(
                f'  exports.Set("{js_name}", Napi::Function::New(env, Wrap_{func.c_name}));'
            )

        # Export classes
        for cls in self.namespace.classes:
            self.output.append(f"  // {cls.name} class")
            self.output.append(
                f"  Napi::Object {cls.name.lower()}_class = Napi::Object::New(env);"
            )

            # Export methods
            for func in cls.functions:
                if func.is_constructor:
                    self.output.append(
                        f'  {cls.name.lower()}_class.Set("new", Napi::Function::New(env, Wrap_{cls.name}_{func.name}));'
                    )
                elif func.is_static:
                    self.output.append(
                        f'  {cls.name.lower()}_class.Set("{func.js_name()}", Napi::Function::New(env, Wrap_{cls.name}_{func.name}));'
                    )
                else:
                    self.output.append(
                        f'  {cls.name.lower()}_class.Set("{func.js_name()}", Napi::Function::New(env, Wrap_{cls.name}_{func.name}));'
                    )

            self.output.append(
                f'  exports.Set("{cls.name}", {cls.name.lower()}_class);'
            )

        self.output.append("  return exports;")
        self.output.append("}")
        self.output.append("")
        self.output.append("NODE_API_MODULE(NODE_GYP_MODULE_NAME, Init)")


class JavaScriptGenerator:
    def __init__(self, namespace: Namespace):
        self.namespace = namespace
        self.output = []
        self.class_names = [cls.name for cls in namespace.classes]
        # Map class name to Class object for parent lookup
        self.class_map = {cls.name: cls for cls in namespace.classes}

    def generate(self) -> str:
        """Generate JavaScript wrapper code"""
        self.output = []
        self.output.append("// Generated by generate_from_gir.py")
        self.output.append("// DO NOT EDIT THIS FILE DIRECTLY")
        self.output.append("")
        self.output.append("const addon = require('./build/Release/flatpak.node');")
        self.output.append("")

        for cls in self.namespace.classes:
            self.generate_class(cls)
            self.output.append("")

        # Compute unique export names for functions
        function_export_map = self._get_function_export_map()

        # Generate standalone functions
        for func in self.namespace.functions:
            export_name = function_export_map[func.c_name]
            self.generate_function_export(func, export_name)
            self.output.append("")

        self.generate_exports(function_export_map)

        return "\n".join(self.output)

    def hyphen_to_camel(self, name: str) -> str:
        """Convert hyphenated string to camelCase"""
        parts = name.split("-")
        if not parts:
            return name
        # First part remains lowercase
        result = parts[0]
        # Capitalize remaining parts
        for part in parts[1:]:
            if part:
                result += part[0].upper() + part[1:]
        return result

    def generate_class(self, cls: Class):
        """Generate JavaScript class wrapper"""
        self.output.append(f"class {cls.name} {{")
        self.output.append("  constructor(handle) {")
        self.output.append("    this._handle = handle;")
        self.output.append("  }")
        self.output.append("")

        # Collect all methods from class hierarchy
        all_methods = self._collect_methods_from_hierarchy(cls)
        all_static_methods = self._collect_static_methods_from_hierarchy(cls)
        all_properties = self._collect_properties_from_hierarchy(cls)

        # Generate static methods
        for func in all_static_methods:
            self.generate_static_method(cls, func)

        # Generate instance methods
        for func in all_methods:
            if not func.is_constructor:
                self.generate_method(cls, func)

        # Generate property getters/setters
        for prop in all_properties:
            if prop.readable:
                self.generate_property_getter(prop)
            if prop.writable:
                self.generate_property_setter(prop)

        self.output.append("")
        self.output.append("  get _native() {")
        self.output.append("    return this._handle;")
        self.output.append("  }")
        self.output.append("")
        self.output.append("  free() {")
        self.output.append("    // Drop our reference to the native handle so the underlying")
        self.output.append("    // GObject can be unref'd by the external finalizer. After this")
        self.output.append("    // the wrapper must not be used again.")
        self.output.append("    this._handle = null;")
        self.output.append("  }")
        self.output.append("")
        self.output.append("  // Explicit resource management: enables `using x = ...` syntax.")
        self.output.append("  [Symbol.dispose]() {")
        self.output.append("    this.free();")
        self.output.append("  }")
        self.output.append("}")
        self.output.append("")

        # Generate constructor factory
        self.generate_constructor_factory(cls)

    def _collect_methods_from_hierarchy(self, cls: Class) -> list:
        """Collect all instance methods from class hierarchy"""
        methods = []
        seen_names = set()

        # Traverse hierarchy
        current = cls
        while current:
            # Add methods from current class (child overrides parent)
            for func in current.functions:
                if not func.is_constructor and not func.is_static:
                    if func.js_name() not in seen_names:
                        methods.append(func)
                        seen_names.add(func.js_name())

            # Move to parent if exists in our generated classes
            if current.parent and current.parent in self.class_map:
                current = self.class_map[current.parent]
            else:
                break

        return methods

    def _collect_static_methods_from_hierarchy(self, cls: Class) -> list:
        """Collect all static methods from class hierarchy"""
        static_methods = []
        seen_names = set()

        # Traverse hierarchy
        current = cls
        while current:
            # Add static methods from current class
            for func in current.functions:
                if func.is_static:
                    if func.js_name() not in seen_names:
                        static_methods.append(func)
                        seen_names.add(func.js_name())

            # Move to parent if exists in our generated classes
            if current.parent and current.parent in self.class_map:
                current = self.class_map[current.parent]
            else:
                break

        return static_methods

    def _collect_properties_from_hierarchy(self, cls: Class) -> list:
        """Collect all properties from class hierarchy"""
        properties = []
        seen_names = set()

        # Traverse hierarchy
        current = cls
        while current:
            # Add properties from current class
            for prop in current.properties:
                if prop.name not in seen_names:
                    properties.append(prop)
                    seen_names.add(prop.name)

            # Move to parent if exists in our generated classes
            if current.parent and current.parent in self.class_map:
                current = self.class_map[current.parent]
            else:
                break

        return properties

    def _find_method_owner(
        self, cls: Class, method_name: str, is_static: bool = False
    ) -> str:
        """Find which class in the hierarchy owns a method"""
        # Traverse hierarchy from child to parent
        current = cls
        while current:
            # Check if method exists in current class
            for func in current.functions:
                if not func.is_constructor:
                    if func.is_static == is_static:
                        if func.js_name() == method_name:
                            return current.name

            # Move to parent if exists in our generated classes
            if current.parent and current.parent in self.class_map:
                current = self.class_map[current.parent]
            else:
                break

        # If not found, return the original class name
        return cls.name

    def generate_method(self, cls: Class, func: Function):
        """Generate instance method"""
        js_name = func.js_name()
        params = ", ".join([p.name for p in func.parameters if not p.is_instance])

        # Find which class actually owns this method
        owner_class = self._find_method_owner(cls, js_name, is_static=False)

        self.output.append(f"  {js_name}({params}) {{")
        self.output.append(
            f"    const result = addon.{owner_class}.{js_name}(this._handle{', ' + params if params else ''});"
        )
        self._generate_array_wrapping(func.return_value, "result")
        self.output.append(f"    return result;")
        self.output.append("  }")
        self.output.append("")

    def generate_static_method(self, cls: Class, func: Function):
        """Generate static method"""
        js_name = func.js_name()
        params = ", ".join([p.name for p in func.parameters])

        # Find which class actually owns this static method
        owner_class = self._find_method_owner(cls, js_name, is_static=True)

        self.output.append(f"  static {js_name}({params}) {{")
        self.output.append(
            f"    const result = addon.{owner_class}.{js_name}({params});"
        )
        self._generate_array_wrapping(func.return_value, "result")
        self.output.append(f"    return result;")
        self.output.append(f"  }}")
        self.output.append("")

    def generate_property_getter(self, prop: Property):
        """Generate property getter"""
        getter_name = prop.getter_name()
        # Convert hyphenated property names to camelCase
        prop_name = self.hyphen_to_camel(prop.name)
        self.output.append(f"  get {prop_name}() {{")
        self.output.append(f"    return this.{getter_name}();")
        self.output.append("  }")
        self.output.append("")

    def generate_property_setter(self, prop: Property):
        """Generate property setter"""
        setter_name = prop.setter_name()
        # Convert hyphenated property names to camelCase
        prop_name = self.hyphen_to_camel(prop.name)
        self.output.append(f"  set {prop_name}(value) {{")
        self.output.append(f"    this.{setter_name}(value);")
        self.output.append("  }")
        self.output.append("")

    def _get_wrapper_class(self, element_type: str) -> str:
        """Return JavaScript wrapper class name for element type"""
        # Map GIR type names to JavaScript class names
        # Some GIR types match JavaScript class names directly
        if element_type in self.class_names:
            return element_type

        # Special cases
        if element_type == "BundleRef":
            return "BundleRef"

        # Check for common Flatpak types that might not be in class_names
        # but are part of the API
        flatpak_types = [
            "InstalledRef",
            "RemoteRef",
            "Remote",
            "Ref",
            "RelatedRef",
            "TransactionOperation",
            "Instance",
            "Installation",
        ]
        if element_type in flatpak_types:
            return element_type

        # For GLib types or unknown types, return None (no wrapper)
        return None

    def _generate_array_wrapping(self, return_value, var_name: str):
        """Generate code to wrap array elements if needed"""
        if return_value.gir_type == "GLib.PtrArray" and return_value.element_type:
            element_type = return_value.element_type
            # Check if element type has a wrapper class
            wrapper_class = self._get_wrapper_class(element_type)
            if wrapper_class:
                self.output.append(f"    if (Array.isArray({var_name})) {{")
                self.output.append(f"      return {var_name}.map(item => {{")
                self.output.append(f"        if (!item) return item;")
                self.output.append(f"        // Check if already wrapped")
                self.output.append(
                    f"        if (item._native !== undefined) return item;"
                )
                self.output.append(f"        // Wrap in appropriate class")
                self.output.append(f"        const wrapperClass = {wrapper_class};")
                self.output.append(
                    f"        return wrapperClass ? new wrapperClass(item) : item;"
                )
                self.output.append(f"      }});")
                self.output.append(f"    }}")

    def generate_constructor_factory(self, cls: Class):
        """Generate constructor factory function"""
        # Find constructors
        constructors = [f for f in cls.functions if f.is_constructor]
        if constructors:
            # Use the first constructor
            constr = constructors[0]
            params = ", ".join([p.name for p in constr.parameters if not p.is_instance])

            self.output.append(f"{cls.name}.create = function({params}) {{")
            self.output.append(f"  const handle = addon.{cls.name}.new({params});")
            self.output.append(f"  return new {cls.name}(handle);")
            self.output.append("};")
            self.output.append("")

    def generate_function_export(self, func: Function, export_name: str = None):
        """Generate standalone function export"""
        if export_name is None:
            export_name = func.js_name()
        params = ", ".join([p.name for p in func.parameters])

        self.output.append(f"function {export_name}({params}) {{")
        self.output.append(f"  const result = addon.{export_name}({params});")
        self._generate_array_wrapping(func.return_value, "result")
        self.output.append(f"  return result;")
        self.output.append("}")
        self.output.append("")

    def _get_function_export_map(self):
        """Return mapping from function to unique export name"""
        exported_names = set()
        mapping = {}
        for func in self.namespace.functions:
            js_name = func.js_name()
            # Unconditionally rename quark functions
            if "quark" in js_name:
                if func.c_name == "flatpak_error_quark":
                    js_name = "errorQuark"
                elif func.c_name == "flatpak_portal_error_quark":
                    js_name = "portalErrorQuark"
            # Handle duplicate function names (after quark renaming)
            if js_name in exported_names:
                # Add prefix to avoid duplicates
                js_name = f"{func.c_name.split('_')[0]}_{js_name}"
            exported_names.add(js_name)
            mapping[func.c_name] = js_name
        return mapping

    def generate_exports(self, function_export_map=None):
        """Generate exports section"""
        self.output.append("// Exports")
        self.output.append("module.exports = {")

        # Export classes
        class_exports = []
        for cls in self.namespace.classes:
            class_exports.append(f"  {cls.name}")

        # Export standalone functions
        function_exports = []
        if function_export_map is None:
            # Fallback: compute map ourselves
            function_export_map = self._get_function_export_map()

        for func in self.namespace.functions:
            export_name = function_export_map[func.c_name]
            function_exports.append(f"  {export_name}")

        # Combine all exports
        all_exports = class_exports + function_exports
        self.output.append(",\n".join(all_exports))
        self.output.append("};")


def main():
    parser = argparse.ArgumentParser(
        description="Generate libflatpak bindings from GIR file"
    )
    parser.add_argument(
        "--gir", default="/usr/share/gir-1.0/Flatpak-1.0.gir", help="Path to GIR file"
    )
    parser.add_argument(
        "--output-cpp", default="src/flatpak.cc", help="Output C++ file"
    )
    parser.add_argument(
        "--output-js", default="index.js", help="Output JavaScript file"
    )
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")

    args = parser.parse_args()

    if not os.path.exists(args.gir):
        print(f"Error: GIR file not found: {args.gir}")
        sys.exit(1)

    print(f"Parsing GIR file: {args.gir}")
    parser = GIRParser(args.gir)
    namespace = parser.parse()

    print(f"Found {len(namespace.classes)} classes")
    print(f"Found {len(namespace.functions)} standalone functions")

    # Generate C++ bindings
    print(f"Generating C++ bindings: {args.output_cpp}")
    cpp_generator = CppGenerator(namespace)
    cpp_code = cpp_generator.generate()

    os.makedirs(os.path.dirname(args.output_cpp), exist_ok=True)
    with open(args.output_cpp, "w") as f:
        f.write(cpp_code)

    # Generate JavaScript bindings
    print(f"Generating JavaScript bindings: {args.output_js}")
    js_generator = JavaScriptGenerator(namespace)
    js_code = js_generator.generate()

    with open(args.output_js, "w") as f:
        f.write(js_code)

    print("Done!")


if __name__ == "__main__":
    main()
