// Active decisions that govern a component: the ones ABOUT it, plus repo-wide ones ABOUT the Repo. Runs on every edit check.
// Anchor on the component first and walk with WITH: as one pattern FalkorDB starts from all decisions (24 ms vs 0.6 ms).
// The two halves use different names inside the [ | ] lists: FalkorDB returns [] when they share one.
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
       [(d)-[:CHOSE]->(a1:Library) | a1.name] AS chose, [(d)-[:REJECTED]->(b1:Library) | b1.name] AS rejected
