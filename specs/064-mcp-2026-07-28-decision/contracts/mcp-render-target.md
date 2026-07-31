# Contract: MCP render target

The MCP target consumes the same validated component dictionaries that other `webrender` targets receive. It never treats component content as raw HTML.

## Projection

| Astral primitive | MCP content block |
|---|---|
| Text, Heading, Markdown, CodeBlock, Alert, Badge, KeyValue, Table, Form, Button, container/layout primitives | `text` containing a deterministic plain-text/Markdown representation. Interactive controls describe their label and disabled/non-executable state; they do not become executable host UI. |
| Image with an allowed inline payload | `image` with Base64 data and MIME type. |
| Audio with an allowed inline payload | `audio` with Base64 data and MIME type. |
| File/resource reference with a safe URI | `resource_link` using the server-issued URI, name, description, MIME type, and size when known. |
| Embedded safe resource | `resource` with text or Base64 blob content. |
| Unknown primitive or one without a faithful representation | `text` naming the primitive type and providing its sanitized readable fields. Nothing is silently dropped. |

Multiple components preserve traversal order. Layout and visual-only CSS do not cross the boundary. HTML, scripts, event handlers, credentials, internal owner identifiers, and unapproved URLs never appear.

## Structured content

`structuredContent` comes from the tool's machine-readable result before component rendering. If the result is a mapping/list/scalar it is copied through the existing safe serialization boundary and must conform to `outputSchema` when one is advertised. Renderer-produced text is not reparsed to invent structured data.

The target is registered as `mcp` through `backend/webrender/registry.py::register_target`; it does not add a client-facing primitive or modify `ui_protocol.json`.

