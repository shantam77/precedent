// People who know a component: commits there (from git), most recent activity first among the top committers.
MATCH (p:Person)-[k:KNOWS]->(c:Component {name: $component})
RETURN p.name AS name, p.emails[0] AS email, k.commits AS commits, k.last AS last
ORDER BY k.commits DESC LIMIT 8
