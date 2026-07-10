"""
src/ingestion — Phase 7 unstructured-input pipelines (scratchpad build).

Turns the outside world's raw signals into structured, schema-checked
inputs for the knowledge graph:

  macro_tracker  Crude / Gold (India vs World) / USDINR levels mapped onto
                 a SHORT / MEDIUM / LONG directional matrix, with a Dhan
                 live path and a strict fail-open local JSON fallback.
  news_parser    local-Ollama semantic parsing of headlines into a strict
                 five-key trading signal frame.

Everything here is advisory-input plumbing: nothing in this package
places, modifies, or proposes trades, and nothing writes to
brain_map.db, the journal, or any live state.
"""
