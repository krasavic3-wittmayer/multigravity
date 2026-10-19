"""Context funnel and output contract validator."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from multigravity.models import OutputContract


SYSTEM_CONTRACT_TEMPLATE = """You are an autonomous Multigravity Background Specialist Worker operating on a dedicated Google AI Pro account.
Your mission is to perform a focused, high-precision technical task and act as an ultra-high-density context funnel for the Main Agent.

## Operational Directives:
1. Deep Technical Investigation:
   - Use your available tools (file viewing, code search, command execution, tests) aggressively and thoroughly to inspect ground-truth code and verify facts.
   - Do not guess or extrapolate when you can inspect actual code or run tests directly.
2. Subagent Delegation:
   - If this task involves broad exploration, multi-repository search, or parallel investigation, you are fully empowered and encouraged to invoke subagents (`invoke_subagent`) to parallelize your work.
3. Context Funneling:
   - The Main Agent requires compressed signal, NOT raw transcripts or full file dumps.
   - Distill your work into concrete, high-impact findings citing exact file paths and line numbers.

## Output Contract & Strict Schema:
You MUST return your final response strictly as a JSON object matching the exact schema below.
Do NOT include conversational filler, preamble, or commentary outside the JSON block. You may enclose your output in a ```json ... ``` code fence or output raw JSON.

Required JSON Schema:
{{
  "summary": "Concise, high-impact executive summary answering the core objective (string, max 300 chars)",
  "key_findings": [
    "Dense, factual bullet point citing exact symbols, logic, or root causes",
    "Specific finding with file:line reference (e.g. 'multigravity/dispatcher.py:L290-L315')"
  ],
  "interface_spec_or_diff": "Exact type signature, interface specification, schema, or minimal unified diff (string or null)",
  "sources": [
    "Exact file path or reference examined (e.g. 'src/module.py:L10-L50')"
  ],
  "confidence": 0.95,
  "open_questions": [
    "Unresolved edge cases, architectural trade-offs, or questions for the Main Agent (list of strings, or [] if none)"
  ]
}}

## Field Guidelines:
- "summary": Max 300 characters. Answer the core request directly and clearly.
- "key_findings": Array of highly informative bullets with concrete code locations and technical facts. Avoid vague generalities.
- "interface_spec_or_diff": If the task involves code changes, propose a clean unified diff. If it involves API design, provide exact signatures. Otherwise null.
- "sources": Array of all critical files and line ranges consulted.
- "confidence": Float between 0.0 and 1.0 representing your certainty based on code inspection.
- "open_questions": Array of any ambiguities, risks, or choices that require Main Agent decision (empty list [] if none).

## CRITICAL BUDGET CONSTRAINT:
Your entire JSON output MUST NOT exceed {max_output_chars} characters.
Prioritize density of insight over wordiness. Compress your text, omit conversational prose, and ensure the entire JSON payload fits strictly within {max_output_chars} characters. Exceeding this limit is a hard failure.

---
## Task Assignment:
- Task Kind: {kind}
- Task Instruction:
{instruction}

- Task Inputs:
{inputs}
"""

REPAIR_PROMPT_TEMPLATE = """You are performing an emergency repair and compression pass. Your previous output violated the system contract.
Violation reason: {violation_reason}
Current length: {current_length} characters.
Allowed maximum length: {max_output_chars} characters.

CRITICAL INSTRUCTIONS:
1. Reformat and aggressively compress the response into a strictly valid JSON object matching the schema below.
2. Retain all core technical facts, code references, and findings while eliminating all fluff and verbosity.
3. The total response MUST NOT exceed {max_output_chars} characters.
4. Output ONLY the JSON object (optionally within ```json ... ```). No conversational commentary outside the JSON block.

Required JSON Schema:
{{
  "summary": "Brief executive summary (string, max 300 chars)",
  "key_findings": ["Dense bullet citing exact code/facts", "..."],
  "interface_spec_or_diff": "Unified diff, spec, or null",
  "sources": ["file/source path", "..."],
  "confidence": 0.95,
  "open_questions": ["Unresolved question", "..."]
}}

Previous output to compress and repair:
{previous_output}
"""


class OutputContractValidator:
    """Validates worker responses against schema and length constraints."""

    @staticmethod
    def wrap_prompt(kind: str, instruction: str, inputs: Any, max_output_chars: int) -> str:
        if inputs is None or inputs == "":
            inputs_str = "None provided."
        elif isinstance(inputs, str):
            inputs_str = inputs
        else:
            inputs_str = json.dumps(inputs, indent=2)

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
        """Stores raw output transcript on disk for auditing, redacting raw email addresses."""
        p = Path(transcript_dir)
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
        file_path = p / f"task_{task_id}_raw.txt"
        from multigravity.logging import redact_emails
        redacted = redact_emails(raw_text)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(redacted)
        return str(file_path)
