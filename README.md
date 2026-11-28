# mcp-dpe-fr

A read-only [Model Context Protocol](https://modelcontextprotocol.io) server over the French **DPE**
(*Diagnostic de Performance Énergétique*) open data published by ADEME: 16 million energy
diagnostics of existing dwellings issued since July 2021. It lets an assistant answer questions such
as *"which départements have the most F-G rated homes?"* with numbers it can cite.

- 6 task-oriented tools, all read-only, aggregates only.
- No API key, no account, no database: it queries the public ADEME API, which aggregates server-side.
- stdio by default, streamable HTTP optional.
- Offline test suite and a 19-question evaluation set with independently computed answers.

## Install

One command, nothing to clone (needs [uv](https://docs.astral.sh/uv/)):

```bash
uvx mcp-dpe-fr
```

Until the package is on PyPI, run it from the repository:

```bash
uvx --from git+https://github.com/julianorg-alt/mcp-dpe-fr mcp-dpe-fr
```

### Claude Code

```bash
claude mcp add dpe-fr -- uvx mcp-dpe-fr
```

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "dpe-fr": { "command": "uvx", "args": ["mcp-dpe-fr"] }
  }
}
```

### HTTP

```bash
uvx mcp-dpe-fr --transport streamable-http --port 8000   # endpoint: http://127.0.0.1:8000/mcp
```

It binds to localhost and has no authentication. The data is public, but put it behind your own
reverse proxy with authentication and a request quota before exposing it.

## Tools

| Tool | Answers |
|---|---|
| `dpe_find_commune` | INSEE code of a commune from its name, with its number of DPE. |
| `dpe_label_distribution` | Count and share per label A to G, share of A-B and of F-G ("passoires thermiques"). |
| `dpe_energy_stats` | Mean, P10, median, P90 of primary energy (kWh/m²/yr), greenhouse gases (kg CO2e/m²/yr) or living area. |
| `dpe_rank_territories` | Rank départements, régions or communes by F-G share, A-B share or mean primary energy. |
| `dpe_heating_energy_mix` | Main energy source (electricity, gas, oil, wood, district heating...). |
| `dpe_trend` | Number of DPE and label mix per year or month. |

The five statistical tools share the same optional filters: one geographic scope (`department`,
`region`, `commune_insee` or `postal_code`), `building_type`, `construction_period`,
`construction_year_min/max`, `climate_zone`, `dpe_date_from/to`. Every response ends with a `source`
block (dataset, licence, latest DPE date) for the model to cite.

### Examples

> *Which départements have the highest share of F-G homes?* → `dpe_rank_territories`

```json
[{"rank": 1, "code": "23", "name": "Creuse",  "dwellings": 23112, "value": 32.7},
 {"rank": 2, "code": "15", "name": "Cantal",  "dwellings": 24910, "value": 29.4},
 {"rank": 3, "code": "48", "name": "Lozère",  "dwellings": 11099, "value": 28.3}]
```

> *What share of homes in Finistère heat with oil?* → `dpe_heating_energy_mix` (`department: "29"`): 9.3 %.

> *Has the share of F-G diagnoses fallen in Paris since 2021?* → `dpe_trend` (`department: "75"`): 30.4 % in
> 2021, 24.9 % in 2022, 18.4 % in 2023, 13.3 % in 2024.

## Privacy by design

The DPE dataset contains postal addresses, building names and coordinates of individual dwellings.
This server **never requests those fields**: it only calls aggregation endpoints and never the row
endpoint. In addition:

- inputs are enumerated or strictly patterned (INSEE codes, ISO dates, fixed lists); no free text
  reaches the upstream query;
- a scope with fewer than 10 dwellings is refused, and any count under 10 is shown as `<10`;
- commune names are the only text returned besides labels and energy types.

The data is already public; the point is that an assistant cannot be used to look up a given
household's diagnosis through this server. A test checks that no recorded upstream call touches an
address, coordinate or DPE-number field.

## Limits

- **What the numbers mean.** The dataset counts diagnoses, not homes: a dwelling can appear several
  times, and sales and rentals trigger diagnoses, so the sample is biased toward transactions. It is
  not a census of the housing stock. `dpe_trend` shows the mix of diagnoses issued.
- **Data quality.** Values are declared by diagnosticians and contain outliers; prefer the median. A
  few commune names are misspelled in the source. Percentiles are approximate (computed by the
  upstream search engine): repeated calls can differ by about 1 %.
- **Paris** is split by arrondissement (75101-75120); use `department: "75"` for the city.
- **Commune ranking** covers the 200 communes with the most DPE in the department.
- **Rate limiting.** The client sends at most 5 requests/s (burst 5, 4 in parallel), retries 429/5xx
  with backoff (honouring `Retry-After`), and caches identical queries for 5 minutes. A typical tool
  call is 2 to 4 upstream requests, about 1 to 2 s. There is no per-user quota in HTTP mode.
- **Errors** are returned as MCP tool errors written for the model (what was wrong, what to do next);
  internal errors are logged on stderr and never forwarded.
- Dataset: *DPE Logements existants (depuis juillet 2021)*, existing dwellings only (not new-build,
  not tertiary buildings).

## Development

```bash
uv sync
uv run pytest -q                              # 45 tests, offline, about 4 s
uv run python evals/run_eval.py --mode replay # evaluation on recorded responses (offline)
uv run python evals/run_eval.py --mode live   # same questions against the real API
uv run python evals/run_eval.py --mode record # refresh cassette and expected values
```

### Evaluation set

`evals/questions.json` holds 19 questions in the style a user would ask, each tied to a tool call.
The reference answer is **recomputed with raw HTTP calls that share no code with the tools**
(counting rows, dividing, taking the argmax), so a bug in a tool cannot validate itself. Questions
cover counts, shares, medians and percentiles, rankings (département, région, commune), energy mix,
year filters, commune lookup, and one refusal (scope under 10 dwellings). Live run: 19/19 in 9 s.
Tolerance is 0 for counts and codes, 0.05 point for shares, 0.5 to 3.5 units for percentiles.

### Tests

Unit tests (input validation, privacy suppression), client tests (retry, 429, 400, cache, rate
limit), MCP protocol tests through an in-memory client, and the whole evaluation replayed from
`tests/cassettes/eval.json`. CI runs them on Python 3.10, 3.12 and 3.13, plus `gitleaks` on the full
history and a weekly live evaluation to catch upstream changes.

## Data licence

Data: **ADEME, "DPE Logements existants (depuis juillet 2021)"**, published on
[data.ademe.fr](https://data.ademe.fr/datasets/dpe03existant) under the **Licence Ouverte / Open
Licence version 2.0** (Etalab). Attribution is required: cite ADEME as the source and the date of
extraction. The recorded responses in `tests/cassettes/` are aggregates derived from that data and
carry the same licence.

The code is MIT-licensed (see `LICENSE`). Not affiliated with ADEME.
