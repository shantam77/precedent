// All the decisions that apply to a component right now.
// That means the ones about this component plus the ones about the whole repo. This runs on every edit an agent makes.
// We find the component first and then walk to its decisions, because starting from all decisions is about 40x slower.
// The two halves use different names inside the [ | ] lists because FalkorDB returns an empty list when they share one.
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
