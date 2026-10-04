FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string"},
                    "laterality": {"type": "string"},
                    "finding": {"type": "string"},
                    "quote": {"type": "string"},
                    "certainty": {
                        "type": "string",
                        "enum": ["confirmed", "suggested", "negated"],
                    },
                    "source_text": {"type": "string"},
                },
                "required": ["section", "finding", "quote", "certainty"],
            },
        }
    },
    "required": ["findings"],
}
