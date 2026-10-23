---
name: render-awsdac-diagram
description: Render or update an architecture diagram in this repo with awslabs/diagram-as-code (awsdac). Use when editing docs/diagrams/*.yaml or articles/diagrams/*.yaml, regenerating the PNGs, or adding a new figure. Carries the schema traps and the awsdac version nuance that bite otherwise.
---

# Render an awsdac diagram

Diagrams in this repo are "diagram as code": a `.yaml` source renders to a `.png` via
`awsdac` (github.com/awslabs/diagram-as-code). Sources + rendered output:

- `docs/diagrams/*.yaml`  -> `docs/diagrams/out/*.png`      (architecture, architecture-refinement)
- `articles/diagrams/*.yaml` -> `articles/diagrams/out/*.png` (per-article figures)

## Procedure

1. Edit the `.yaml` source (never hand-edit the `.png`).
2. Render:
   ```
   awsdac <path>/<figure>.yaml -o <path>/out/<figure>.png -f
   ```
   `-f` overwrites without prompting. Run it from the repo root.
3. VIEW the resulting PNG to confirm it is correct (nodes, labels, flows) before committing.
   Minor label overlap is a known cosmetic awsdac layout quirk, not a content error.
4. Commit the `.yaml` AND the regenerated `.png` together.

## Installing awsdac if it is absent (Linux/CI)

`awsdac` is a Go binary. If `command -v awsdac` fails and Go is present:
```
export GOBIN=$PWD/bin GOPATH=$PWD/go GOCACHE=$PWD/gocache
go install github.com/awslabs/diagram-as-code/cmd/awsdac@latest
export PATH=$PWD/bin:$PATH
```
(The operator Windows box has v0.23 on PATH at `C:\Users\mbonnet\tools\awsdac`.)

## Schema traps (these WILL bite)

- Region blocks are `AWS::Region`, NOT `AWS::Diagram::Region`.
- Set `Direction: vertical` on a container to stack rows (default is horizontal).
- Use `Type: orthogonal` on a link for right-angled routing.
- Multi-line titles are not supported (`\n` is ignored) -- keep titles single-line.
- AWS Batch nodes use `Type: AWS::Batch`.

## VERSION NUANCE -- link label keys (learned live 2026-10-22)

`articles.md` documents `Labels: { AutoRight: { Title: ... } }` / `AutoLeft`. Those keys are
REJECTED by awsdac v0.22.4 (the version `go install ...@latest` currently pulls) with:
`field AutoLeft not found in type ctl.LinkLabels`. Use the supported keys instead:
`SourceLeft` / `SourceRight` / `TargetLeft` / `TargetRight`. If you hit that decode error on an
existing file, it was authored for an older awsdac -- replace `AutoLeft`->`SourceLeft` and
`AutoRight`->`SourceRight` and it renders.

## Verify (completion, not exit code)

Confirm the PNG's modified time advanced and open it. A zero exit from `awsdac` with an
unchanged image means nothing rendered.
