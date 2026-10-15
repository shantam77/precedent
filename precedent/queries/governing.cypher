// Active decisions that govern a component: the ones ABOUT it, plus repo-wide ones (ABOUT the Repo node).
// Planner note (measured): anchor on the component and put a WITH barrier before the expansion. Written as one
// pattern, FalkorDB starts from a scan of ALL decisions (24 ms at 19k decisions) instead of the component (0.6 ms).
// Path-scoped decisions (d.paths globs) are ABOUT every component under the glob's prefix (store.scope);
// tools.governing() keeps only those whose globs match the file.
// Distinct variable names in the pattern comprehensions: FalkorDB returns [] if two of them share one.
MATCH (x:Component) WHERE x.name = $component
WITH x MATCH (x)<-[:ABOUT]-(d:Decision) WHERE d.status = 'active'
RETURN d.id AS id, d.title AS title, d.rationale AS rationale, d.trust AS trust, d.version AS version,
       d.forbid AS forbid, d.paths AS paths, d.source AS source, d.valid_from AS since, d.locked AS locked,
       [(d)-[:CHOSE]->(c1:Library) | c1.name] AS chose, [(d)-[:REJECTED]->(r1:Library) | r1.name] AS rejected
UNION
MATCH (x:Repo)
WITH x MATCH (x)<-[:ABOUT]-(d:Decision) WHERE d.status = 'active'
RETURN d.id AS id, d.title AS title, d.rationale AS rationale, d.trust AS trust, d.version AS version,
       d.forbid AS forbid, d.paths AS paths, d.source AS source, d.valid_from AS since, d.locked AS locked,
       [(d)-[:CHOSE]->(c2:Library) | c2.name] AS chose, [(d)-[:REJECTED]->(r2:Library) | r2.name] AS rejected
