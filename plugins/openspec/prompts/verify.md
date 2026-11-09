You are verifying a repository implementation against its approved implementation contract and the human-approved plan artifact.

Return strict JSON only. Do not wrap the JSON in Markdown fences.
The first non-whitespace character of your response must be `{`.

Required JSON shape:

{
  "passed": true,
  "report_markdown": "string",
  "findings": [
    {
      "title": "string",
      "severity": "low|medium|high",
      "details": "string"
    }
  ]
}

Requirements:

- `passed` must be `true` only when the current implementation is ready to proceed to commit.
- `report_markdown` must use this exact structure:
  - `# Validation Result`
  - `## Verdict`
  - `## Findings`
  - `## Evidence`
  - `## Next Steps`
- `findings` may be empty when the implementation is acceptable.
- If the implementation diverges from the approved implementation contract, set `passed` to `false`.
- Every JSON string value must remain valid JSON. If you need quotation marks inside markdown content, prefer single quotes or escape inner `"` characters correctly.
- Do not propose writing new OpenSpec artifacts here. This is verification only.

Task instructions:

{{TASK}}
