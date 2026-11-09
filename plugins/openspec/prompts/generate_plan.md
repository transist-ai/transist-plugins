You are preparing OpenSpec change artifacts for a Transist workflow.

Return strict JSON only. Do not wrap the JSON in Markdown fences.
The first non-whitespace character of your response must be `{`.

Required JSON shape:

{
  "summary_markdown": "string",
  "proposal_markdown": "string",
  "tasks_markdown": "string",
  "design_markdown": "string",
  "specs": [
    {
      "capability": "string",
      "deltas": [
        {
          "type": "ADDED|MODIFIED|REMOVED|RENAMED",
          "requirements": [
            {
              "title": "string",
              "body_markdown": "string",
              "scenarios": [
                {
                  "title": "string",
                  "body_markdown": "string"
                }
              ]
            }
          ]
        }
      ]
    }
  ]
}

Requirements:

- `summary_markdown` must be the human-facing plan artifact and use this exact structure:
  1. Goal
  2. Constraints and assumptions
  3. Implementation plan
  4. Verification plan
  5. Risks and open questions
- `proposal_markdown` must be a valid OpenSpec proposal for change `{{CHANGE_NAME}}`.
- `tasks_markdown` must be a valid OpenSpec tasks checklist for change `{{CHANGE_NAME}}`.
- `design_markdown` must be a non-empty design document for change `{{CHANGE_NAME}}`.
- Keep `design_markdown` concise when the change is simple, but still include a real `design.md` artifact.
- `specs` must contain at least one capability with at least one delta-bearing requirement.
- Every `specs[].capability` must be a filesystem-safe slug without `/`, `..`, or spaces.
- Every `specs[].deltas[].type` must be exactly one of `ADDED`, `MODIFIED`, `REMOVED`, or `RENAMED`.
- Every requirement must have at least one scenario.
- Every `requirements[].body_markdown` must be requirement content only, not its own `### Requirement` heading.
- Every `scenarios[].body_markdown` must be scenario content only, not its own `#### Scenario` heading.
- Use concrete requirement language in `requirements[].body_markdown`, preferably `SHALL` or `MUST`.
- Each scenario body should describe conditions and expected outcomes in 1-3 short bullet points or short paragraphs.
- Every JSON string value must remain valid JSON. If you need quotation marks inside markdown content, prefer single quotes or escape inner `"` characters correctly.
- Do not describe running tools or changing files yourself. Produce content only.
- Do not emit `spec_markdown`; the plugin will render `spec.md` files from the structured delta data.

Task instructions:

{{TASK}}
