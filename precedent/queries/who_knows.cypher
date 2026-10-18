// Look for all the people who may have worked
// on this component using the commit history and then order by commit volume

MATCH (p:Person)-[k:KNOWS]->(c:Component {name: $component})
RETURN p.name AS name, p.emails[0] as email, k.commits as commits,k.last as last
ORDER BY k.commits DESC LIMIT 8
