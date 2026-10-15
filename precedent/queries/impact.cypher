// What depends on this component, up to 3 hops, over dependencies that are still current (no valid_to),
// with the active decisions and the top person who knows each dependent.
MATCH p = (dep:Component)-[:DEPENDS_ON*1..3]->(:Component {name: $component})
WHERE all(r IN relationships(p) WHERE r.valid_to IS NULL)
WITH dep, min(length(p)) AS hops
OPTIONAL MATCH (d:Decision {status: 'active'})-[:ABOUT]->(dep)
RETURN dep.name AS component, hops, collect(DISTINCT d.id) AS decisions
ORDER BY hops, component LIMIT 40
