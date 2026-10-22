"""Immutable model responses with validation feedback and resumable repairs."""
import json
from jsonschema import ValidationError
import providers
from core import atomic_json, read_json


def validated_candidate(path, payload, system, validator):
    """Reuse valid responses; a rejected cache must not poison every retry.

    At most one corrective request per invocation, five per candidate. Every
    raw response survives, including unsuccessful repairs, before validation.
    """
    error = None
    paths = [path] + [path.with_name(path.stem+'.repair-'+str(i).zfill(3)+path.suffix)
                      for i in range(1,6)]
    for candidate in paths:
        value = read_json(candidate)
        generated = value is None
        if generated:
            request = payload if error is None else {
                'evidence':payload, 'previous_response':previous,
                'validation_error':error,
                'instruction':'修正 JSON 格式和候选分类；只用本批提供的候选编号，保留有依据的内容，不虚构事实。'}
            value = providers.summarize(request,system)
            atomic_json(candidate,value)
        try:
            return validator(value)
        except (providers.ProviderError,ValidationError) as exc:
            previous = value
            if isinstance(exc,ValidationError):
                error = 'schema_'+str(exc.validator)+' at '+json.dumps(list(exc.absolute_path))
            else:
                error = str(exc)
            if generated:
                raise providers.ProviderError(error) from None
    raise providers.ProviderError(error or 'model_output_repair_exhausted')


def declared_fields(value, schema):
    """Ignore unsolicited metadata; never coerce or invent required values."""
    if isinstance(value,dict) and schema.get('additionalProperties') is False:
        properties=schema.get('properties',{})
        return {key:declared_fields(item,properties[key]) for key,item in value.items() if key in properties}
    if isinstance(value,list) and isinstance(schema.get('items'),dict):
        return [declared_fields(item,schema['items']) for item in value]
    return value
