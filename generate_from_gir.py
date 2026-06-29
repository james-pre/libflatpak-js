#!/usr/bin/env python3
"""
Generate libflatpak bindings from GObject Introspection (GIR) file.

This script parses Flatpak-1.0.gir and generates:
1. C++ N-API ObjectWrap classes in src/flatpak.cc
2. The ESM entry point in index.js (re-exporting the native addon)
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
        self.class_map = {cls.name: cls for cls in namespace.classes}
        # Set of class names we generate as ObjectWrap classes.
        self.our_classes = set(self.class_map.keys())
        # Statement used to bail out of parameter extraction on a type error.
        # Functions/methods/accessor-getters return Napi::Value; the class
        # constructor returns void, so it overrides this to a bare `return;`.
        self.param_fail_return = "return env.Null();"

    def is_our_class(self, name: str) -> bool:
        """True if `name` (a GIR/short class name) is one of our ObjectWrap classes."""
        return name in self.our_classes

    def gir_to_our_class(self, gir_type: str):
        """Return our ObjectWrap class name for a GIR type, or None.

        Handles both "Flatpak.Remote" and bare "Remote" forms.
        """
        if not gir_type:
            return None
        short = gir_type.split(".")[-1]
        if short in self.our_classes:
            return short
        return None

    def primary_constructor(self, cls: Class):
        """Return the constructor bound to JS `new`, or None.

        Prefers the plain `new` constructor; otherwise the first constructor.
        """
        constructors = [f for f in cls.functions if f.is_constructor]
        if not constructors:
            return None
        for c in constructors:
            if c.name == "new":
                return c
        return constructors[0]

    def secondary_constructors(self, cls: Class):
        """Constructors exposed as static factory methods (all but the primary)."""
        primary = self.primary_constructor(cls)
        return [
            f for f in cls.functions if f.is_constructor and f is not primary
        ]

    def static_factory_js_name(self, func: Function) -> str:
        """JS name for a constructor exposed as a static factory (e.g. newForPath)."""
        parts = func.name.split("_")
        return parts[0] + "".join(p.capitalize() for p in parts[1:])

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

        self.generate_class_declarations()
        self.output.append("")
        self.generate_standalone_forward_decls()
        self.output.append("")
        self.generate_class_definitions()
        self.output.append("")
        self.generate_standalone_definitions()
        self.output.append("")
        self.generate_init_function()

        return "\n".join(self.output)

    # ------------------------------------------------------------------
    # Hierarchy helpers
    # ------------------------------------------------------------------

    def _collect_methods_from_hierarchy(self, cls: Class) -> list:
        """Collect all instance methods from the class hierarchy (child wins)."""
        methods = []
        seen = set()
        current = cls
        while current:
            for func in current.functions:
                if not func.is_constructor and not func.is_static:
                    if func.js_name() not in seen:
                        methods.append((current, func))
                        seen.add(func.js_name())
            if current.parent and current.parent in self.class_map:
                current = self.class_map[current.parent]
            else:
                break
        return methods

    def _collect_static_methods_from_hierarchy(self, cls: Class) -> list:
        """Collect all static methods from the class hierarchy."""
        statics = []
        seen = set()
        current = cls
        while current:
            for func in current.functions:
                if func.is_static:
                    if func.js_name() not in seen:
                        statics.append((current, func))
                        seen.add(func.js_name())
            if current.parent and current.parent in self.class_map:
                current = self.class_map[current.parent]
            else:
                break
        return statics

    def _collect_properties_from_hierarchy(self, cls: Class) -> list:
        """Collect all properties from the class hierarchy."""
        properties = []
        seen = set()
        current = cls
        while current:
            for prop in current.properties:
                if prop.name not in seen:
                    properties.append(prop)
                    seen.add(prop.name)
            if current.parent and current.parent in self.class_map:
                current = self.class_map[current.parent]
            else:
                break
        return properties

    def _instance_method_for_property(self, cls: Class, accessor_name: str):
        """Find the (owner, Function) implementing a property's get_/set_ accessor."""
        current = cls
        while current:
            for func in current.functions:
                if (
                    not func.is_constructor
                    and not func.is_static
                    and func.name == accessor_name
                ):
                    return (current, func)
            if current.parent and current.parent in self.class_map:
                current = self.class_map[current.parent]
            else:
                break
        return None

    # ------------------------------------------------------------------
    # Class declarations
    # ------------------------------------------------------------------

    def generate_class_declarations(self):
        """Emit the ObjectWrap class declarations for every Flatpak class."""
        for cls in self.namespace.classes:
            methods = self._collect_methods_from_hierarchy(cls)
            statics = self._collect_static_methods_from_hierarchy(cls)
            properties = self._collect_properties_from_hierarchy(cls)
            has_ctor = self.primary_constructor(cls) is not None

            self.output.append(
                f"class {cls.name} final : public Napi::ObjectWrap<{cls.name}> {{"
            )
            self.output.append("public:")
            self.output.append("  static Napi::FunctionReference constructor;")
            self.output.append(
                "  static void Init(Napi::Env env, Napi::Object& exports);"
            )
            self.output.append(
                f"  static Napi::Object NewInstance(Napi::Env env, {cls.c_name}* handle);"
            )
            self.output.append(f"  {cls.name}(const Napi::CallbackInfo& info);")
            self.output.append(f"  ~{cls.name}();")
            self.output.append(f"  {cls.c_name}* handle_ = nullptr;")
            self.output.append("")
            self.output.append("private:")
            self.output.append("  static bool constructing;")
            self.output.append(f"  {cls.c_name}* self(Napi::Env env);")
            self.output.append("")

            # Static factory methods (secondary constructors).
            for func in self.secondary_constructors(cls):
                self.output.append(
                    f"  static Napi::Value {func.name}_factory(const Napi::CallbackInfo& info);"
                )
            # Standalone static methods.
            for owner, func in statics:
                self.output.append(
                    f"  static Napi::Value {func.js_name()}(const Napi::CallbackInfo& info);"
                )
            # Instance methods (including inherited).
            for owner, func in methods:
                self.output.append(
                    f"  Napi::Value {func.js_name()}(const Napi::CallbackInfo& info);"
                )
            # Property accessors.
            for prop in properties:
                getter = self._instance_method_for_property(cls, prop.getter_name())
                setter = self._instance_method_for_property(cls, prop.setter_name())
                prop_id = self.hyphen_to_underscore(prop.name)
                if prop.readable and getter:
                    self.output.append(
                        f"  Napi::Value prop_get_{prop_id}(const Napi::CallbackInfo& info);"
                    )
                if prop.writable and setter:
                    self.output.append(
                        f"  void prop_set_{prop_id}(const Napi::CallbackInfo& info, const Napi::Value& value);"
                    )

            self.output.append("};")
            self.output.append("")
            _ = has_ctor  # constructor handling lives in the definition

    def hyphen_to_underscore(self, name: str) -> str:
        return name.replace("-", "_")

    def hyphen_to_camel(self, name: str) -> str:
        """Convert a hyphenated property name to camelCase."""
        parts = name.split("-")
        if not parts:
            return name
        result = parts[0]
        for part in parts[1:]:
            if part:
                result += part[0].upper() + part[1:]
        return result

    def _error_param_name(self, func: Function):
        """Return the GError parameter name for a throwing function, or None."""
        for param in func.parameters:
            if param.is_error_param():
                return param.name
        if func.throws:
            return "error"
        return None

    def generate_standalone_forward_decls(self):
        """Forward declarations for standalone (namespace-level) functions."""
        for func in self.namespace.functions:
            self.output.append(
                f"Napi::Value Wrap_{func.c_name}(const Napi::CallbackInfo& info);"
            )

    # ------------------------------------------------------------------
    # Class definitions
    # ------------------------------------------------------------------

    def generate_class_definitions(self):
        """Emit member definitions for every class."""
        for cls in self.namespace.classes:
            self.output.append(
                f"Napi::FunctionReference {cls.name}::constructor;"
            )
            self.output.append(f"bool {cls.name}::constructing = false;")
            self.output.append("")
            self.generate_class_init(cls)
            self.generate_class_new_instance(cls)
            self.generate_class_constructor(cls)
            self.generate_class_destructor(cls)
            self.generate_class_self(cls)

            for func in self.secondary_constructors(cls):
                self.generate_factory_definition(cls, func)
            for owner, func in self._collect_static_methods_from_hierarchy(cls):
                self.generate_static_method_definition(cls, owner, func)
            for owner, func in self._collect_methods_from_hierarchy(cls):
                self.generate_method_definition(cls, owner, func)
            for prop in self._collect_properties_from_hierarchy(cls):
                self.generate_property_definitions(cls, prop)

    def generate_class_init(self, cls: Class):
        methods = self._collect_methods_from_hierarchy(cls)
        statics = self._collect_static_methods_from_hierarchy(cls)
        properties = self._collect_properties_from_hierarchy(cls)

        self.output.append(
            f"void {cls.name}::Init(Napi::Env env, Napi::Object& exports) {{"
        )
        self.output.append(
            f'  Napi::Function func = DefineClass(env, "{cls.name}", {{'
        )
        entries = []
        for owner, func in methods:
            entries.append(
                f'    InstanceMethod("{func.js_name()}", &{cls.name}::{func.js_name()})'
            )
        for prop in properties:
            getter = self._instance_method_for_property(cls, prop.getter_name())
            setter = self._instance_method_for_property(cls, prop.setter_name())
            prop_name = self.hyphen_to_camel(prop.name)
            prop_id = self.hyphen_to_underscore(prop.name)
            g = (
                f"&{cls.name}::prop_get_{prop_id}"
                if (prop.readable and getter)
                else "nullptr"
            )
            s = (
                f"&{cls.name}::prop_set_{prop_id}"
                if (prop.writable and setter)
                else "nullptr"
            )
            if g == "nullptr" and s == "nullptr":
                continue
            entries.append(
                f'    InstanceAccessor("{prop_name}", {g}, {s})'
            )
        for func in self.secondary_constructors(cls):
            entries.append(
                f'    StaticMethod("{self.static_factory_js_name(func)}", &{cls.name}::{func.name}_factory)'
            )
        for owner, func in statics:
            entries.append(
                f'    StaticMethod("{func.js_name()}", &{cls.name}::{func.js_name()})'
            )
        self.output.append(",\n".join(entries))
        self.output.append("  });")
        self.output.append("")
        self.output.append("  constructor = Napi::Persistent(func);")
        self.output.append("  constructor.SuppressDestruct();")
        self.output.append(f'  exports.Set("{cls.name}", func);')
        self.output.append("}")
        self.output.append("")

    def generate_class_new_instance(self, cls: Class):
        self.output.append(
            f"Napi::Object {cls.name}::NewInstance(Napi::Env env, {cls.c_name}* handle) {{"
        )
        self.output.append("  Napi::EscapableHandleScope scope(env);")
        self.output.append("  constructing = true;")
        self.output.append("  Napi::Object obj;")
        self.output.append("  try {")
        self.output.append("    obj = constructor.New({});")
        self.output.append("  } catch (...) {")
        self.output.append("    constructing = false;")
        self.output.append("    throw;")
        self.output.append("  }")
        self.output.append("  constructing = false;")
        self.output.append("")
        self.output.append(f"  {cls.name}* wrapper = {cls.name}::Unwrap(obj);")
        self.output.append("  if (handle && G_IS_OBJECT(handle)) {")
        self.output.append("    g_object_ref(handle);")
        self.output.append("  }")
        self.output.append("  wrapper->handle_ = handle;")
        self.output.append("  return scope.Escape(obj).As<Napi::Object>();")
        self.output.append("}")
        self.output.append("")

    def generate_class_constructor(self, cls: Class):
        primary = self.primary_constructor(cls)
        self.output.append(
            f"{cls.name}::{cls.name}(const Napi::CallbackInfo& info)"
        )
        self.output.append(f"    : Napi::ObjectWrap<{cls.name}>(info) {{")
        self.output.append("  Napi::Env env = info.Env();")
        self.output.append("  if (constructing) {")
        self.output.append("    return;")
        self.output.append("  }")
        self.output.append("")
        if primary is None:
            self.output.append(
                f'  Napi::TypeError::New(env, "{cls.name} objects cannot be constructed directly").ThrowAsJavaScriptException();'
            )
            self.output.append("  return;")
            self.output.append("}")
            self.output.append("")
            return

        # Build the GObject from JS args via the primary constructor.
        # The constructor returns void, so a failed type check must `return;`.
        self.param_fail_return = "return;"
        cpp_params = []
        js_index = 0
        for param in primary.parameters:
            if param.is_instance:
                continue
            self.generate_parameter_code(param, js_index, cpp_params)
            js_index += 1
        self.param_fail_return = "return env.Null();"

        error_param_name = self._error_param_name(primary)
        if error_param_name:
            self.output.append(f"  GError* {error_param_name} = NULL;")

        call = f"  {cls.c_name}* handle = {primary.c_name}("
        call += ", ".join(cpp_params)
        if error_param_name:
            call += (", &" if cpp_params else "&") + error_param_name
        call += ");"
        self.output.append(call)
        self.output.append("")
        if error_param_name:
            self.output.append(f"  if ({error_param_name}) {{")
            self.output.append(
                f"    Napi::Error::New(env, {error_param_name}->message).ThrowAsJavaScriptException();"
            )
            self.output.append(f"    g_error_free({error_param_name});")
            self.output.append("    return;")
            self.output.append("  }")
        self.output.append("  handle_ = handle;")
        self.output.append("}")
        self.output.append("")

    def generate_class_destructor(self, cls: Class):
        self.output.append(f"{cls.name}::~{cls.name}() {{")
        self.output.append("  if (handle_ && G_IS_OBJECT(handle_)) {")
        self.output.append("    g_object_unref(handle_);")
        self.output.append("  }")
        self.output.append("  handle_ = nullptr;")
        self.output.append("}")
        self.output.append("")

    def generate_class_self(self, cls: Class):
        self.output.append(f"{cls.c_name}* {cls.name}::self(Napi::Env env) {{")
        self.output.append("  if (!handle_) {")
        self.output.append(
            f'    Napi::Error::New(env, "{cls.name} has not been initialized").ThrowAsJavaScriptException();'
        )
        self.output.append("  }")
        self.output.append("  return handle_;")
        self.output.append("}")
        self.output.append("")

    def _emit_call_and_return(self, func: Function, cpp_params):
        """Emit the C call, error handling and return conversion.

        `cpp_params` already contains the leading `self` argument for instance
        methods (and is empty / arg-only for static/standalone/factory).
        Used by every kind of wrapper body.
        """
        error_param_name = self._error_param_name(func)
        if error_param_name:
            self.output.append(f"  GError* {error_param_name} = NULL;")

        if func.return_value.c_type == "void":
            call_line = f"  {func.c_name}("
            call_line += ", ".join(cpp_params)
            if error_param_name:
                call_line += (", &" if cpp_params else "&") + error_param_name
            call_line += ");"
            self.output.append(call_line)
            self.output.append("")
            result_var = None
        else:
            result_var = "result"
            call_line = f"  {func.return_value.c_type} {result_var} = {func.c_name}("
            call_line += ", ".join(cpp_params)
            if error_param_name:
                call_line += (", &" if cpp_params else "&") + error_param_name
            call_line += ");"
            self.output.append(call_line)
            self.output.append("")

        if error_param_name:
            self.output.append(f"  if ({error_param_name}) {{")
            self.output.append(
                f"    Napi::Error::New(env, {error_param_name}->message).ThrowAsJavaScriptException();"
            )
            self.output.append(f"    g_error_free({error_param_name});")
            self.output.append("    return env.Null();")
            self.output.append("  }")
            self.output.append("")

        self.generate_return_conversion(
            func.return_value, result_var if result_var is not None else ""
        )

    def generate_method_definition(self, cls: Class, owner: Class, func: Function):
        """Emit an instance method member definition."""
        self.output.append(
            f"Napi::Value {cls.name}::{func.js_name()}(const Napi::CallbackInfo& info) {{"
        )
        self.output.append("  Napi::Env env = info.Env();")
        self._emit_self(cls, owner)
        self.output.append("  if (!self) {")
        self.output.append("    return env.Null();")
        self.output.append("  }")
        self.output.append("")

        cpp_params = ["self"]
        js_param_index = 0
        for param in func.parameters:
            if param.is_instance:
                continue
            self.generate_parameter_code(param, js_param_index, cpp_params)
            js_param_index += 1

        self._emit_call_and_return(func, cpp_params)
        self.output.append("}")
        self.output.append("")

    def _emit_self(self, cls: Class, owner: Class):
        """Emit the `self` local, typed for the class that declares the C call.

        For inherited methods/accessors the C function expects the parent type,
        so we upcast the leaf handle (a GObject is-a its parent).
        """
        if owner is cls:
            self.output.append(f"  {cls.c_name}* self = this->self(env);")
        else:
            self.output.append(
                f"  {owner.c_name}* self = reinterpret_cast<{owner.c_name}*>(this->self(env));"
            )

    def generate_static_method_definition(self, cls: Class, owner: Class, func: Function):
        """Emit a static method member definition."""
        self.output.append(
            f"Napi::Value {cls.name}::{func.js_name()}(const Napi::CallbackInfo& info) {{"
        )
        self.output.append("  Napi::Env env = info.Env();")
        self.output.append("")

        cpp_params = []
        for i, param in enumerate(func.parameters):
            self.generate_parameter_code(param, i, cpp_params)

        self._emit_call_and_return(func, cpp_params)
        self.output.append("}")
        self.output.append("")

    def generate_factory_definition(self, cls: Class, func: Function):
        """Emit a static factory method that wraps a secondary constructor."""
        self.output.append(
            f"Napi::Value {cls.name}::{func.name}_factory(const Napi::CallbackInfo& info) {{"
        )
        self.output.append("  Napi::Env env = info.Env();")
        self.output.append("")

        cpp_params = []
        js_index = 0
        for param in func.parameters:
            if param.is_instance:
                continue
            self.generate_parameter_code(param, js_index, cpp_params)
            js_index += 1

        error_param_name = self._error_param_name(func)
        if error_param_name:
            self.output.append(f"  GError* {error_param_name} = NULL;")

        call = f"  {cls.c_name}* handle = {func.c_name}("
        call += ", ".join(cpp_params)
        if error_param_name:
            call += (", &" if cpp_params else "&") + error_param_name
        call += ");"
        self.output.append(call)
        self.output.append("")
        if error_param_name:
            self.output.append(f"  if ({error_param_name}) {{")
            self.output.append(
                f"    Napi::Error::New(env, {error_param_name}->message).ThrowAsJavaScriptException();"
            )
            self.output.append(f"    g_error_free({error_param_name});")
            self.output.append("    return env.Null();")
            self.output.append("  }")
            self.output.append("")
        self.output.append("  if (!handle) {")
        self.output.append("    return env.Null();")
        self.output.append("  }")
        self.output.append(f"  Napi::Object obj = {cls.name}::NewInstance(env, handle);")
        self.output.append("  // NewInstance takes its own ref; drop the constructor's.")
        self.output.append("  if (G_IS_OBJECT(handle)) {")
        self.output.append("    g_object_unref(handle);")
        self.output.append("  }")
        self.output.append("  return obj;")
        self.output.append("}")
        self.output.append("")

    def generate_property_definitions(self, cls: Class, prop: Property):
        """Emit accessor member definitions for a property."""
        prop_id = self.hyphen_to_underscore(prop.name)
        getter = self._instance_method_for_property(cls, prop.getter_name())
        setter = self._instance_method_for_property(cls, prop.setter_name())

        if prop.readable and getter:
            gowner, gfunc = getter
            self.output.append(
                f"Napi::Value {cls.name}::prop_get_{prop_id}(const Napi::CallbackInfo& info) {{"
            )
            self.output.append("  Napi::Env env = info.Env();")
            self._emit_self(cls, gowner)
            self.output.append("  if (!self) {")
            self.output.append("    return env.Null();")
            self.output.append("  }")
            self.output.append("")
            cpp_params = ["self"]
            self._emit_call_and_return(gfunc, cpp_params)
            self.output.append("}")
            self.output.append("")

        if prop.writable and setter:
            sowner, sfunc = setter
            self.output.append(
                f"void {cls.name}::prop_set_{prop_id}(const Napi::CallbackInfo& info, const Napi::Value& value) {{"
            )
            self.output.append("  Napi::Env env = info.Env();")
            self._emit_self(cls, sowner)
            self.output.append("  if (!self) {")
            self.output.append("    return;")
            self.output.append("  }")
            self.output.append("")
            cpp_params = ["self"]
            # The InstanceAccessor setter receives the value as `value`, not via
            # info[]; extract the single value parameter inline.
            value_param = None
            for param in sfunc.parameters:
                if param.is_instance or param.is_error_param():
                    continue
                value_param = param
                break
            self._emit_setter_value(value_param, cpp_params)
            error_param_name = self._error_param_name(sfunc)
            if error_param_name:
                self.output.append(f"  GError* {error_param_name} = NULL;")
            call = f"  {sfunc.c_name}("
            call += ", ".join(cpp_params)
            if error_param_name:
                call += (", &" if cpp_params else "&") + error_param_name
            call += ");"
            self.output.append(call)
            if error_param_name:
                self.output.append(f"  if ({error_param_name}) {{")
                self.output.append(
                    f"    Napi::Error::New(env, {error_param_name}->message).ThrowAsJavaScriptException();"
                )
                self.output.append(f"    g_error_free({error_param_name});")
                self.output.append("    return;")
                self.output.append("  }")
            self.output.append("}")
            self.output.append("")

    def _emit_setter_value(self, param: Parameter, cpp_params):
        """Extract a property setter's single value from `value`."""
        if param is None:
            return
        if param.gir_type in ("utf8", "filename"):
            self.output.append("  std::string value_str = value.As<Napi::String>().Utf8Value();")
            self.output.append(f"  const char* {param.name} = value_str.c_str();")
        elif param.gir_type == "gboolean":
            self.output.append(f"  gboolean {param.name} = value.As<Napi::Boolean>().Value();")
        elif param.is_enum():
            ctype = param.c_type.rstrip("*").strip()
            self.output.append(
                f"  {ctype} {param.name} = static_cast<{ctype}>(value.As<Napi::Number>().Int32Value());"
            )
        elif param.gir_type in (
            "gint", "guint", "gint8", "guint8", "gint16", "guint16",
            "gint32", "guint32", "gint64", "guint64", "glong", "gulong",
            "gshort", "gushort", "gsize", "gssize", "gdouble", "gfloat",
        ):
            if "64" in param.gir_type:
                self.output.append(
                    f"  {param.c_type} {param.name} = value.As<Napi::Number>().Int64Value();"
                )
            elif "double" in param.gir_type or "float" in param.gir_type:
                self.output.append(
                    f"  {param.c_type} {param.name} = value.As<Napi::Number>().DoubleValue();"
                )
            else:
                self.output.append(
                    f"  {param.c_type} {param.name} = value.As<Napi::Number>().Int32Value();"
                )
        else:
            self.output.append(
                f"  // Unsupported setter value type '{param.gir_type}' for '{param.name}'"
            )
            self.output.append(f"  return;")
            return
        cpp_params.append(param.name)

    def generate_standalone_definitions(self):
        """Emit definitions for standalone (namespace-level) functions."""
        for func in self.namespace.functions:
            self.generate_function_definition(func)

    def generate_function_definition(self, func: Function):
        """Emit a standalone function wrapper definition."""
        self.output.append(
            f"Napi::Value Wrap_{func.c_name}(const Napi::CallbackInfo& info) {{"
        )
        self.output.append("  Napi::Env env = info.Env();")
        self.output.append("")
        cpp_params = []
        for i, param in enumerate(func.parameters):
            self.generate_parameter_code(param, i, cpp_params)
        self._emit_call_and_return(func, cpp_params)
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
                self.output.append(f"      {self.param_fail_return}")
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
                self.output.append(f"    {self.param_fail_return}")
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
            self.output.append(f"    {self.param_fail_return}")
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
            self.output.append(f"    {self.param_fail_return}")
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
            self.output.append(f"    {self.param_fail_return}")
            self.output.append("  }")
            # Remove pointer if present in c_type (treat as regular enum)
            c_type_without_ptr = param.c_type.rstrip("*").strip()
            self.output.append(
                f"  {c_type_without_ptr} {param.name} = static_cast<{c_type_without_ptr}>(info[{index}].As<Napi::Number>().Int32Value());"
            )
            cpp_params.append(param.name)

        elif param.is_gobject():
            # Resolve the concrete C base type.
            base_type = param.c_type.rstrip("*").strip()
            if base_type == "":
                short = param.gir_type.split(".")[-1]
                if short in [
                    "File",
                    "Cancellable",
                    "Bytes",
                    "HashTable",
                    "KeyFile",
                    "Variant",
                    "List",
                    "PtrArray",
                ]:
                    base_type = "G" + short
                else:
                    base_type = "Flatpak" + short

            our_class = self.gir_to_our_class(param.gir_type)
            if our_class is None and base_type.startswith("Flatpak"):
                # c_type-derived Flatpak type; check if it's one of ours.
                our_class = self.gir_to_our_class(base_type[len("Flatpak"):])

            if our_class is not None:
                # One of our ObjectWrap classes: unwrap to its native handle.
                if param.nullable:
                    self.output.append(f"  {param.c_type} {param.name} = NULL;")
                    self.output.append(
                        f"  if (info.Length() > {index} && !info[{index}].IsNull() && !info[{index}].IsUndefined()) {{"
                    )
                    self.output.append(f"    if (!info[{index}].IsObject()) {{")
                    self.output.append(
                        f"      Napi::TypeError::New(env, \"Expected {our_class} or null for parameter '{param.name}'\").ThrowAsJavaScriptException();"
                    )
                    self.output.append(f"      {self.param_fail_return}")
                    self.output.append("    }")
                    self.output.append(
                        f"    {param.name} = {our_class}::Unwrap(info[{index}].As<Napi::Object>())->handle_;"
                    )
                    self.output.append("  }")
                else:
                    self.output.append(
                        f"  if (info.Length() <= {index} || !info[{index}].IsObject()) {{"
                    )
                    self.output.append(
                        f"    Napi::TypeError::New(env, \"Expected {our_class} for parameter '{param.name}'\").ThrowAsJavaScriptException();"
                    )
                    self.output.append(f"    {self.param_fail_return}")
                    self.output.append("  }")
                    self.output.append(
                        f"  {param.c_type} {param.name} = {our_class}::Unwrap(info[{index}].As<Napi::Object>())->handle_;"
                    )
                cpp_params.append(param.name)
            else:
                # Foreign GObject / boxed type: still passed as an External handle.
                if param.nullable:
                    self.output.append(f"  {param.c_type} {param.name} = NULL;")
                    self.output.append(
                        f"  if (info.Length() > {index} && !info[{index}].IsNull() && !info[{index}].IsUndefined()) {{"
                    )
                    self.output.append(f"    if (!info[{index}].IsExternal()) {{")
                    self.output.append(
                        f"      Napi::TypeError::New(env, \"Expected external object or null for parameter '{param.name}'\").ThrowAsJavaScriptException();"
                    )
                    self.output.append(f"      {self.param_fail_return}")
                    self.output.append("    }")
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
                    self.output.append(f"    {self.param_fail_return}")
                    self.output.append("  }")
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
                if self.is_our_class(element_type):
                    # One of our ObjectWrap classes: wrap as a real instance.
                    c_type = f"Flatpak{element_type}*"
                    self.output.append(
                        f"      {c_type} typed_item = static_cast<{c_type}>(item);"
                    )
                    self.output.append(f"      if (!typed_item) {{")
                    self.output.append(f"        js_array.Set(i, env.Null());")
                    self.output.append(f"        continue;")
                    self.output.append(f"      }}")
                    self.output.append(
                        f"      js_array.Set(i, {element_type}::NewInstance(env, typed_item));"
                    )
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

                    our_class = self.gir_to_our_class(return_value.gir_type)
                    if our_class is None and base_type.startswith("Flatpak"):
                        our_class = self.gir_to_our_class(base_type[len("Flatpak"):])

                    if our_class is not None:
                        # Wrap as a real ObjectWrap instance of our class.
                        self.output.append(f"  if (!{var_name}) {{")
                        self.output.append(f"    return env.Null();")
                        self.output.append(f"  }}")
                        self.output.append(
                            f"  Napi::Object js_obj = {our_class}::NewInstance(env, {var_name});"
                        )
                        if return_value.transfer in ["full", "container"]:
                            # NewInstance took its own ref; release the one we own.
                            self.output.append(f"  if (G_IS_OBJECT({var_name})) {{")
                            self.output.append(f"    g_object_unref({var_name});")
                            self.output.append(f"  }}")
                        self.output.append(f"  return js_obj;")
                    else:
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

    def function_export_names(self):
        """Yield (func, js_export_name) for every standalone function.

        Applies the same quark renaming and duplicate-prefixing used when the
        functions are registered on `exports`, so the C++ init and the JS
        entry point always agree on the exported names.
        """
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
            yield func, js_name

    def generate_init_function(self):
        """Generate the N-API module initialization function"""
        self.output.append("Napi::Object Init(Napi::Env env, Napi::Object exports) {")

        # Export standalone functions with duplicate handling
        for func, js_name in self.function_export_names():
            self.output.append(
                f'  exports.Set("{js_name}", Napi::Function::New(env, Wrap_{func.c_name}));'
            )

        # Register the ObjectWrap classes.
        for cls in self.namespace.classes:
            self.output.append(f"  {cls.name}::Init(env, exports);")

        self.output.append("  return exports;")
        self.output.append("}")
        self.output.append("")
        self.output.append("NODE_API_MODULE(NODE_GYP_MODULE_NAME, Init)")

    def generate_js(self) -> str:
        """Generate the ESM entry point (index.js).

        The names mirror exactly what `Init` registers on `exports`: one
        binding per class plus one per standalone function. Both a default
        export (the whole addon) and named exports are provided.
        """
        class_names = [cls.name for cls in self.namespace.classes]
        function_names = [name for _, name in self.function_export_names()]

        lines = []
        lines.append("// Generated by generate_from_gir.py")
        lines.append("// DO NOT EDIT THIS FILE DIRECTLY")
        lines.append("//")
        lines.append("// All classes and functions are implemented natively as")
        lines.append("// Napi::ObjectWrap classes in the addon (src/flatpak.cc). This file")
        lines.append("// loads the compiled addon and re-exports it. ESM cannot `require`")
        lines.append("// directly, so we build a `require` with `createRequire`.")
        lines.append("")
        lines.append('import { createRequire } from "node:module";')
        lines.append("")
        lines.append('const addon = createRequire(import.meta.url)("./build/Release/flatpak.node");')
        lines.append("")
        lines.append("export const {")
        lines.append("  // Classes")
        for name in class_names:
            lines.append(f"  {name},")
        lines.append("  // Functions")
        for name in function_names:
            lines.append(f"  {name},")
        lines.append("} = addon;")
        lines.append("")
        lines.append("export default addon;")
        lines.append("")
        return "\n".join(lines)


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
        "--output-js", default="index.js", help="Output JavaScript entry point"
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

    # Generate JavaScript entry point
    print(f"Generating JavaScript entry point: {args.output_js}")
    js_code = cpp_generator.generate_js()
    with open(args.output_js, "w") as f:
        f.write(js_code)

    print("Done!")


if __name__ == "__main__":
    main()
