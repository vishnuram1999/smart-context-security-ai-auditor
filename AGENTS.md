# UI changes

- Preserve the dashboard’s orange and metallic-grey theme unless the user requests a change.
- Before confirming a UI change, inspect the rendered UI at desktop and narrow/mobile widths. Check spacing between panels and controls, alignment, wrapping, and horizontal overflow; tests and builds alone are not visual validation.
- Use mocked provider responses and an isolated browser profile for visual checks. Do not trigger paid inference or access user credentials.
- If rendered visual inspection is unavailable, say so explicitly; do not claim the UI has been visually verified.
