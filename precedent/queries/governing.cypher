// Active decisions that govern a component: the ones ABOUT it, plus repo-wide ones (ABOUT the Repo node).
// Path-scoped decisions (d.paths globs) are matched in Python, see tools.governing().
MATCH (d:Decision {status: 'active'})-[:ABOUT]->(x)
WHERE (x:Component AND x.name = $component) OR x:Repo
RETURN d.id AS id, d.title AS title, d.rationale AS rationale, d.trust AS trust, d.version AS version,
       d.forbid AS forbid, d.paths AS paths, d.source AS source, d.valid_from AS since, d.locked AS locked,
       [(d)-[:CHOSE]->(c1:Library) | c1.name] AS chose, [(d)-[:REJECTED]->(r1:Library) | r1.name] AS rejected  // distinct vars: FalkorDB returns [] if two comprehensions share one
