# -*- coding: utf-8 -*-
# Copyright 2018-2020 Streamlit Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import mimetypes
import os
from typing import Any, Dict, Optional, Type, Union, Callable
import threading
import inspect

import tornado.web

import streamlit.server.routes
from streamlit import type_util
from streamlit.DeltaGenerator import DeltaGenerator
from streamlit.DeltaGenerator import NoValue
from streamlit.DeltaGenerator import _get_widget_ui_value
from streamlit.elements import arrow_table
from streamlit.errors import StreamlitAPIException
from streamlit.logger import get_logger
from streamlit.proto.ComponentInstance_pb2 import ArgsDataframe
from streamlit.proto.Element_pb2 import Element

LOGGER = get_logger(__name__)


class MarshallComponentException(StreamlitAPIException):
    """Class for exceptions generated during custom component marshalling."""

    pass


# mypy doesn't support *args or **kwargs in Callable declarations, so this
# is as close as we can get to a type for our _custom_wrapper type.
ComponentCallable = Callable[..., Any]


class CustomComponent:
    """A Custom Component declaration."""

    def __init__(
        self, name: str, path: Optional[str] = None, url: Optional[str] = None,
    ):
        if (path is None and url is None) or (path is not None and url is not None):
            raise StreamlitAPIException(
                "Either 'path' or 'url' must be set, but not both."
            )

        self.name = name
        self.path = path
        self.url = url

    @property
    def abspath(self) -> Optional[str]:
        """The absolute path that the component is served from."""
        if self.path is None:
            return None
        return os.path.abspath(self.path)

    def __call__(
        self,
        *args,
        loc: Optional[DeltaGenerator] = None,
        default: Optional[Any] = None,
        key: Optional[str] = None,
        **kwargs,
    ) -> Optional[Any]:
        """An alias for create_instance."""
        return self.create_instance(*args, loc=loc, default=default, key=key, **kwargs)

    def create_instance(
        self,
        *args,
        loc: Optional[DeltaGenerator] = None,
        default: Optional[Any] = None,
        key: Optional[str] = None,
        **kwargs,
    ) -> Optional[Any]:
        """Create a new instance of the component.

        Parameters
        ----------
        *args
            Must be empty; all args must be named. (This parameter exists to
            enforce incorrect use of the function.)
        loc: DeltaGenerator or None
            The DeltaGenerator to write the component to. If unspecified,
            this defaults to st._main
        default: any or None
            The default return value for the component. This is returned when
            the component's frontend hasn't yet specified a value with
            `setComponentValue`.
        key: str or None
            If not None, this is the user key we use to generate the
            component's "widget ID".
        **kwargs
            Keyword args to pass to the component.

        Returns
        -------
        any or None
            The component's widget value.

        """
        if len(args) > 0:
            raise MarshallComponentException(f"Argument '{args[0]}' needs a label")

        # If loc is unspecified, we write to the main DeltaGenerator
        if loc is None:
            loc = streamlit._main

        args_json = {}
        args_df = {}
        for arg_name, arg_val in kwargs.items():
            if type_util.is_dataframe_like(arg_val):
                args_df[arg_name] = arg_val
            else:
                args_json[arg_name] = arg_val

        try:
            serialized_args_json = json.dumps(args_json)
        except BaseException as e:
            raise MarshallComponentException(
                "Could not convert component args to JSON", e
            )

        def marshall_component(element: Element) -> Union[Any, Type[NoValue]]:
            element.component_instance.component_name = self.name
            if self.url is not None:
                element.component_instance.url = self.url

            # Normally, a widget's element_hash (which determines
            # its identity across multiple runs of an app) is computed
            # by hashing the entirety of its protobuf. This means that,
            # if any of the arguments to the widget are changed, Streamlit
            # considers it a new widget instance and it loses its previous
            # state.
            #
            # However! If a *component* has a `key` argument, then the
            # component's hash identity is determined by entirely by
            # `component_name + url + key`. This means that, when `key`
            # exists, the component will maintain its identity even when its
            # other arguments change, and the component's iframe won't be
            # remounted on the frontend.
            #
            # So: if `key` is None, we marshall the element's arguments
            # *before* computing its widget_ui_value (which creates its hash).
            # If `key` is not None, we marshall the arguments *after*.

            def marshall_element_args():
                element.component_instance.args_json = serialized_args_json
                for key, value in args_df.items():
                    new_args_dataframe = ArgsDataframe()
                    new_args_dataframe.key = key
                    arrow_table.marshall(new_args_dataframe.value.data, value)
                    element.component_instance.args_dataframe.append(new_args_dataframe)

            if key is None:
                marshall_element_args()

            widget_value = _get_widget_ui_value(
                element_type="component_instance",
                element=element,
                user_key=key,
                widget_func_name=self.name,
            )

            if key is not None:
                marshall_element_args()

            if widget_value is None:
                widget_value = default

            # widget_value will be either None or whatever the component's most
            # recent setWidgetValue value is. We coerce None -> NoValue,
            # because that's what _enqueue_new_element_delta expects.
            return widget_value if widget_value is not None else NoValue

        result = loc._enqueue_new_element_delta(
            marshall_element=marshall_component, delta_type="component"
        )

        return result

    def __eq__(self, other):
        """Equality operator."""
        return (
            isinstance(other, CustomComponent)
            and self.name == other.name
            and self.path == other.path
            and self.url == other.url
        )

    def __ne__(self, other):
        """Inequality operator."""
        return not self == other

    def __str__(self):
        return f"'{self.name}': {self.path if self.path is not None else self.url}"


def declare_component(
    path: Optional[str] = None, url: Optional[str] = None,
) -> CustomComponent:
    """Declare a new custom component."""

    # 1. Get our stack frame.
    current_frame = inspect.currentframe()
    assert current_frame is not None

    # 2. Get the stack frame of our calling function.
    caller_frame = current_frame.f_back
    assert caller_frame is not None

    # 3. Get the caller's module name.
    module = inspect.getmodule(caller_frame)
    assert module is not None
    component_name = module.__name__

    # 4. If the caller was the main module that was executed (that is,
    # if the user executed `python my_component.py`), then this name will be
    # "__main__" instead of the actual package name. In this case, we use
    # the main module's filename, minus its extension, as the component name.
    if component_name == "__main__":
        file_path = inspect.getfile(caller_frame)
        filename = os.path.basename(file_path)
        component_name, _ = os.path.splitext(filename)

    # Create our component object, and register it.
    component = CustomComponent(name=component_name, path=path, url=url)
    ComponentRegistry.instance().register_component(component)

    return component


class ComponentRequestHandler(tornado.web.RequestHandler):
    def initialize(self, registry: "ComponentRegistry"):
        self._registry = registry

    def get(self, path: str) -> None:
        parts = path.split("/")
        component_name = parts[0]
        component_root = self._registry.get_component_path(component_name)
        if component_root is None:
            self.write(f"{path} not found")
            self.set_status(404)
            return

        filename = "/".join(parts[1:])
        abspath = os.path.join(component_root, filename)

        LOGGER.debug("ComponentRequestHandler: GET: %s -> %s", path, abspath)

        try:
            with open(abspath, "r") as file:
                contents = file.read()
        except OSError as e:
            self.write(f"{path} read error: {e}")
            self.set_status(404)
            return

        self.write(contents)
        self.set_header("Content-Type", self.get_content_type(abspath))

        self.set_extra_headers(path)

    def set_extra_headers(self, path):
        """Disable cache for HTML files.

        Other assets like JS and CSS are suffixed with their hash, so they can
        be cached indefinitely.
        """
        is_index_url = len(path) == 0

        if is_index_url or path.endswith(".html"):
            self.set_header("Cache-Control", "no-cache")
        else:
            self.set_header("Cache-Control", "public")

    def set_default_headers(self) -> None:
        if streamlit.server.routes.allow_cross_origin_requests():
            self.set_header("Access-Control-Allow-Origin", "*")

    def options(self) -> None:
        """/OPTIONS handler for preflight CORS checks."""
        self.set_status(204)
        self.finish()

    @staticmethod
    def get_content_type(abspath):
        """Returns the ``Content-Type`` header to be used for this request.
        From tornado.web.StaticFileHandler.
        """
        mime_type, encoding = mimetypes.guess_type(abspath)
        # per RFC 6713, use the appropriate type for a gzip compressed file
        if encoding == "gzip":
            return "application/gzip"
        # As of 2015-07-21 there is no bzip2 encoding defined at
        # http://www.iana.org/assignments/media-types/media-types.xhtml
        # So for that (and any other encoding), use octet-stream.
        elif encoding is not None:
            return "application/octet-stream"
        elif mime_type is not None:
            return mime_type
        # if mime_type not detected, use application/octet-stream
        else:
            return "application/octet-stream"

    @staticmethod
    def get_url(file_id: str) -> str:
        """Return the URL for a component file with the given ID."""
        return "components/{}".format(file_id)


class ComponentRegistry:
    _instance_lock = threading.Lock()
    _instance = None  # type: Optional[ComponentRegistry]

    @classmethod
    def instance(cls) -> "ComponentRegistry":
        """Returns the singleton ComponentRegistry"""
        # We use a double-checked locking optimization to avoid the overhead
        # of acquiring the lock in the common case:
        # https://en.wikipedia.org/wiki/Double-checked_locking
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = ComponentRegistry()
        return cls._instance

    def __init__(self):
        self._components = {}  # type: Dict[str, CustomComponent]
        self._lock = threading.Lock()

    def register_component(self, component: CustomComponent) -> None:
        """Register a CustomComponent.

        Parameters
        ----------
        component : CustomComponent
            The component to register.
        """

        # Validate the component's path
        abspath = component.abspath
        if abspath is not None and not os.path.isdir(abspath):
            raise StreamlitAPIException(f"No such component directory: '{abspath}'")

        with self._lock:
            existing = self._components.get(component.name)
            self._components[component.name] = component

        if existing is not None and component != existing:
            LOGGER.warning(
                "%s overriding previously-registered %s", component, existing,
            )

        LOGGER.info("Registered component %s", component)

    def get_component_path(self, name: str) -> Optional[str]:
        """Return the filesystem path for the component with the given name.

        If no such component is registered, or if the component exists but is
        being served from a URL, return None instead.
        """
        component = self._components.get(name, None)
        return component.abspath if component is not None else None
