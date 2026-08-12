"""HTTP API — a thin driver over the service layer, parallel to the CLI.

The CLI (`src/cli.py`) and this API are two adapters over the same
`EvidenceGatherer` + `IncidentDiagnoser`; no business logic lives here. See
`app.py` for the FastAPI app and `python -m src serve` to run it.
"""
