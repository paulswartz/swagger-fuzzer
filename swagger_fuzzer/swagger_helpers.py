""" Helpers for swagger specifications
"""
import json
import hypothesis.strategies as st
from copy import copy
from hypothesis.extra.datetime import datetimes
from requests import Request
from furl import furl
from hypothesis import assume
from urllib.parse import quote

from .utils import CustomJsonEncoder

SWAGGER_FORMAT_MAPPING = {
    'int64': st.integers(),
    'integer': st.integers(),
    'int32': st.integers(),
    'number': st.one_of(st.integers(), st.floats()),
    'date-time': datetimes(),
    'date': st.dates(),
    'time': st.times(),
    'boolean': st.booleans(),
}


def _is_swagger_parameter(dict):
    """ Check if the parameter dict is a valid swagger parameter
    """
    return dict.get('type') or dict.get('schema') or dict.get('$ref')


def get_ref(ref, spec):
    assert ref.startswith('#/')
    splitted = ref.lstrip('#/').split('/')

    referenced_to = spec

    for path in splitted:
        referenced_to = referenced_to[path]

    return referenced_to


def get_item_path_acceptable_format(path_item, spec):
    if path_item.get('consumes'):
        return path_item['consumes']

    return spec.get('consumes')


def _get_filtered_parameter(path_item, in_, common_parameters, spec):
    parameters = path_item.get('parameters') + common_parameters
    filtered_params = [p for p in parameters if p['in'] == in_]
    non_converted_params = {p['name']: p for p in filtered_params}
    return CustomTransformation(get_ref, spec).transform(non_converted_params)


def get_request(data, spec, spec_host, settings):
    endpoints = sorted(spec['paths'].keys())
    endpoint_path = data.draw(st.sampled_from(endpoints))
    endpoint = spec['paths'][endpoint_path]

    methods = sorted(set(endpoint.keys()))
    if 'parameters' in methods:
        methods.remove('parameters')
        common_parameters = endpoint['parameters']
    else:
        common_parameters = []

    method_name = data.draw(st.sampled_from(methods))
    endpoint = endpoint[method_name]

    path_params = _get_filtered_parameter(endpoint, 'path', common_parameters, spec)
    path_args = data.draw(st.fixed_dictionaries(path_params))

    query_params = _get_filtered_parameter(endpoint, 'query', common_parameters, spec)
    query_args = data.draw(st.fixed_dictionaries(query_params))

    body_params = _get_filtered_parameter(endpoint, 'body', common_parameters, spec)
    if body_params:
        body_args = data.draw(st.fixed_dictionaries(body_params))
    else:
        body_args = None

    valid_request_body_format = get_item_path_acceptable_format(endpoint, spec)

    request_data = None
    request_headers = copy(settings.headers)

    if body_args:
        # no_body_format_declaration(body_args, valid_request_body_format, endpoint)
        if body_args and valid_request_body_format is None:
            # Force a request format, swagger ui seems to force json format
            valid_request_body_format = ["application/json"]

        request_body_format = data.draw(st.sampled_from(valid_request_body_format), 'request_body_format')

        request_headers['Content-Type'] = request_body_format
        if request_body_format == 'application/x-www-form-urlencoded':
            request_data = body_args
        elif request_body_format == 'application/json':
            request_data = json.dumps(body_args, cls=CustomJsonEncoder)
        elif request_body_format == 'application/xml':
            raise NotImplementedError(request_body_format)
            # TODO Implement XML
        else:
            raise Exception(request_body_format)

    # encode the path values
    path_args = {k: quote(v, safe="") for (k, v) in path_args.items()}

    endpoint_url = endpoint_path.format(**path_args)
    assume('\x00' not in endpoint_url)

    # Generate request
    URL = furl(spec_host).add(path=endpoint_url.lstrip('/'))

    if query_args:
        URL = URL.add(args=query_args)

    request = Request(method_name, URL.url, data=request_data,
                      headers=request_headers).prepare()
    request.build_context = locals()
    return request


class CustomTransformation(object):

    def __init__(self, get_ref, spec):
        self.get_ref = get_ref
        self.spec = spec

    def transform(self, obj):
        """
        """
        obj = self._recursive_transform(obj)
        if isinstance(obj, (list, tuple)):
            return self._transform_array(obj)
        if isinstance(obj, dict):
            return self._transform_dict(obj)
        else:
            return self._transform_obj(obj)

    def _transform_array(self, obj):
        new_array = []
        for index, value in enumerate(obj):
            new_array.append(self.transform(value))

        return new_array

    def _transform_dict(self, obj):
        new_dict = {}
        for key, value in obj.items():
            new_dict[key] = self.transform(value)

        return new_dict

    def _transform_obj(self, obj):
        return self.default(obj)

    def _recursive_transform(self, obj):
        old_obj = None

        while old_obj is not obj:
            old_obj = obj
            obj = self.default(obj)

        return obj

    def default(self, obj):
        """
        """
        if isinstance(obj, dict) and _is_swagger_parameter(obj):
            parameter_type = obj.get('format', obj.get('type'))
            parameter_schema = obj.get('schema')
            parameter_ref = obj.get('$ref')
            parameter_enum = obj.get('enum')

            if parameter_enum:
                return st.sampled_from(parameter_enum)
            if parameter_type in SWAGGER_FORMAT_MAPPING:
                return SWAGGER_FORMAT_MAPPING[parameter_type]
            elif parameter_ref:
                return self.transform(self.get_ref(parameter_ref, self.spec))
            elif parameter_type == 'string':
                if 'pattern' in obj:
                    return st.from_regex(obj['pattern'])
                else:
                    return st.text()
            elif parameter_type == 'array':
                if obj['items'].get('enum'):
                    return st.lists(elements=st.sampled_from(obj['items']['enum']))
                elif obj['items'].get('type'):
                    if obj['items']['type'] == 'object':
                        return st.lists(elements=st.fixed_dictionaries({}))
                    return st.lists(elements=SWAGGER_FORMAT_MAPPING[obj['items']['type']])
                elif obj['items'].get('$ref'):
                    schema = self.get_ref(obj['items']['$ref'], self.spec)
                    return st.lists(elements=self.transform(schema))
                raise Exception('array', obj)
            elif parameter_type == 'object':
                properties = {}
                for property_name, property_ in obj.get('properties', {}).items():
                    properties[property_name] = self.transform(property_)
                return st.fixed_dictionaries(properties)
            elif parameter_schema:
                if parameter_schema.get('type') == 'array':
                    schema = self.get_ref(parameter_schema['items']['$ref'], self.spec)
                    return st.lists(elements=self.transform(schema))
                else:
                    schema = self.get_ref(parameter_schema['$ref'], self.spec)
                    transformed = self.transform(schema)
                    return transformed
            else:
                raise Exception("Invalid", obj, parameter_type)

        return obj
