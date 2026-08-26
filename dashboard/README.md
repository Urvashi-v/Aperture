# `dashboard/` — findings UI

**Status: not started.** Week 4, Days 23–24, hard time-box.

Plain HTML, CSS and vanilla JavaScript in separate files — `index.html`,
`styles.css`, `app.js` — talking to the REST API with `fetch()`. No framework,
no bundler, no component library. This is a deliberate deviation from
DESIGN.md v1.0, recorded in [`../DESIGN.md`](../DESIGN.md) §15.1.

Three views: endpoint list → trace waterfall → findings detail.

**No hardcoded data.** Every number rendered here comes from the API, which
gets it from real analysis of real traces. A dashboard with plausible-looking
placeholder data in it is worse than no dashboard.
