// Follow a decision's history both ways, what it replaced and what later replaced it.
MATCH (d:Decision {id: $id})
OPTIONAL MATCH back = (d)-[:SUPERSEDES*1..10]->(older:Decision)
OPTIONAL MATCH fwd  = (newer:Decision)-[:SUPERSEDES*1..10]->(d)
RETURN d, collect(DISTINCT older) AS older, collect(DISTINCT newer) AS newer
