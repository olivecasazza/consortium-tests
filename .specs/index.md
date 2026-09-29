# .specs

Planning and audit material for this repo, following the
[context-engineering-kit](https://github.com/NeoLabHQ/context-engineering-kit)
convention. Nothing here is built or executed — it is the record of what is
known about the repo's open problems and what has been decided about them.

## Layout

```
.specs/
  index.md          this file
  tasks/            one file per unit of work, bucketed by status
    roadmap.md      checkbox index of everything not finished yet
    todo/           scoped and accepted, not started
    draft/          being scoped
    in-progress/    being implemented
    done/           finished; kept for history
  analysis/         investigations — what is actually in the code
  research/         external material: upstream docs, issues, vendored sources
  reports/          results: audit write-ups, migration scorecards
```

## How work moves

An item is born in `draft/` once it is understood well enough to scope, moves to
`todo/` when it is agreed, to `in-progress/` when someone picks it up, and to
`done/` when it lands. `tasks/roadmap.md` is the index: it holds a checkbox per
open item and is the first thing to read to see what is outstanding. Tick the box
when the work lands, and move the file into `done/`.

## Ground rules for anything recorded here

`tests/`, `lib/`, `conf/`, `bash_completion.d/`, `setup.py`, `MANIFEST.in` and
`packaging/` are mirrored from upstream ClusterShell by the sync tooling. Their
paths are frozen. A finding in one of those files is fixed upstream or by
narrowing this repo's copy — never by editing the mirrored file in place, which
the next sync silently reverts.

Findings should name the file and, where it matters, the line. A backlog entry
nobody can locate is a backlog entry nobody picks up.
