"""Context funnel and output contract validator."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from multigravity.models import OutputContract


SYSTEM_CONTRACT_TEMPLATE = """You are a specialized sub-agent performing a bounded background task.
You must return your output strictly as a JSON object matching the exact schema below.
Do NOT include preamble, conversational filler, or unparsed markdown outside the JSON block.

Required JSON Schema:
{{
  "summary": "Brief executive summary (string, max 300 chars)",
  "key_findings": ["Bullet point 1", "Bullet point 2"],
  "interface_spec_or_diff": "Optional interface specification, signature, or unified diff (string or null)",
  "sources": ["source 1", "source 2"],
  "confidence": 0.95,
  "open_questions": ["Any unresolved question or ambiguity (list of strings)"]
}}

CRITICAL CONSTRAINT:
Your entire JSON output MUST NOT exceed {max_output_chars} characters.
Compress your findings. Never dump raw documents, logs, or multi-page transcripts.

Task Kind: {kind}
Task Instruction:
{instruction}

Inputs:
{inputs}
"""

REPAIR_PROMPT_TEMPLATE = """The previous output violated the system contract.
Violation reason: {violation_reason}
Current length: {current_length} characters.
Allowed maximum length: {max_output_chars} characters.

Compress and reformat the previous response into a strictly valid JSON object matching:
{{
  "summary": "string",
  "key_findings": ["string"],
  "interface_spec_or_diff": "string or null",
  "sources": ["string"],
  "confidence": float,
  "open_questions": ["string"]
}}

Do NOT exceed {max_output_chars} characters.

Previous output:
{previous_output}
"""


class OutputContractValidator:
    """Validates worker responses against schema and length constraints."""

    @staticmethod
    def wrap_prompt(kind: str, instruction: str, inputs: Any, max_output_chars: int) -> str:
        inputs_str = json.dumps(inputs, indent=2) if not isinstance(inputs, str) else inputs
        return SYSTEM_CONTRACT_TEMPLATE.format(
            kind=kind,
            instruction=instruction,
            inputs=inputs_str,
            max_output_chars=max_output_chars,
        )

    @staticmethod
    def build_repair_prompt(previous_output: str, violation_reason: str, max_output_chars: int) -> str:
        return REPAIR_PROMPT_TEMPLATE.format(
            previous_output=previous_output[:10000],  # truncated safely
            violation_reason=violation_reason,
            current_length=len(previous_output),
            max_output_chars=max_output_chars,
        )

    @classmethod
    def extract_json(cls, raw_text: str) -> dict[str, Any] | None:
        raw_text = raw_text.strip()
        # 1. Try direct parsing
        try:
            parsed = json.loads(raw_text)
            if isinstance(parsed, dict):
                if "response" in parsed and isinstance(parsed["response"], str) and "summary" not in parsed:
                    inner = cls.extract_json(parsed["response"])
                    if inner:
                        return inner
                return parsed
        except json.JSONDecodeError:
            pass

        # 2. Try markdown fence ```json ... ``` or ``` ... ```
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw_text)
        if fence_match:
            try:
                parsed = json.loads(fence_match.group(1).strip())
                if isinstance(parsed, dict):
                    if "response" in parsed and isinstance(parsed["response"], str) and "summary" not in parsed:
                        inner = cls.extract_json(parsed["response"])
                        if inner:
                            return inner
                    return parsed
            except json.JSONDecodeError:
                pass

        # 3. Try finding outermost { ... }
        first_brace = raw_text.find("{")
        last_brace = raw_text.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            try:
                parsed = json.loads(raw_text[first_brace : last_brace + 1])
                if isinstance(parsed, dict):
                    if "response" in parsed and isinstance(parsed["response"], str) and "summary" not in parsed:
                        inner = cls.extract_json(parsed["response"])
                        if inner:
                            return inner
                    return parsed
            except json.JSONDecodeError:
                pass

        return None

    @classmethod
    def validate_and_parse(
        cls, raw_output: str, max_chars: int
    ) -> tuple[bool, OutputContract | None, str | None]:
        """Validates raw worker output against contract and max length.
        
        Returns (is_valid, contract, error_message).
        """
        if not raw_output or not raw_output.strip():
            return False, None, "Output is empty"

        data = cls.extract_json(raw_output)
        if not data:
            return False, None, "Failed to parse valid JSON object from output"

        # Validate mandatory schema keys
        if "summary" not in data or not isinstance(data["summary"], str):
            return False, None, "Missing or invalid 'summary' (must be a string)"

        if "key_findings" not in data or not isinstance(data["key_findings"], list):
            return False, None, "Missing or invalid 'key_findings' (must be a list)"

        # Check serialized size
        contract = OutputContract.from_dict(data)
        serialized = json.dumps(contract.to_dict(), separators=(",", ":"))
        if len(serialized) > max_chars:
            return (
                False,
                None,
                f"Serialized output length ({len(serialized)} chars) exceeds maximum allowed ({max_chars} chars)",
            )

        return True, contract, None

    @staticmethod
    def save_raw_transcript(task_id: str, transcript_dir: str | Path, raw_text: str) -> str:
        """Stores raw output transcript on disk for auditing, returns path."""
        p = Path(transcript_dir)
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
        file_path = p / f"task_{task_id}_raw.txt"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(raw_text)
        return str(file_path)
