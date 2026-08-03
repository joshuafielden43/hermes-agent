import pytest

from gateway.platforms.api_server import (
    StructuredOutputValidationError,
    _structured_output_contract,
    _validated_structured_output,
)


def test_structured_output_contract_normalizes_and_validates_without_retrieval():
    contract = _structured_output_contract(
        {
            "type": "json_schema",
            "name": "answer",
            "schema": {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        responses=True,
    )

    assert _validated_structured_output(contract, '```json\n{"ok": true}\n```') == '{"ok": true}'
    with pytest.raises(StructuredOutputValidationError):
        _validated_structured_output(contract, '{"ok": "yes"}')
    with pytest.raises(ValueError, match="external references"):
        _structured_output_contract(
            {"type": "json_schema", "name": "answer", "schema": {"$ref": "https://example.test/schema"}},
            responses=True,
        )
