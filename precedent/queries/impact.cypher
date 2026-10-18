// What could break if $component changes: components that import it, up to 3 hops away over current dependencies,
// with the active decisions on each one. Nearest first.
MATCH p = (dep:Component)-[:DEPENDS_ON*1..3]->(:Component {name: $component})
WHERE all(r IN relationships(p) WHERE r.valid_to IS NULL)
WITH dep, min(length(p)) AS hops
OPTIONAL MATCH (d:Decision {status: 'active'})-[:ABOUT]->(dep)
RETURN dep.name AS component, hops, collect(DISTINCT d.id) AS decisions
ORDER BY hops, component LIMIT 40
