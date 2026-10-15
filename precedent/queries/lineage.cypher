// The supersession chain through one decision: what it replaced and what replaced it (both directions).
MATCH (d:Decision {id: $id})
OPTIONAL MATCH back = (d)-[:SUPERSEDES*1..10]->(older:Decision)
OPTIONAL MATCH fwd = (newer:Decision)-[:SUPERSEDES*1..10]->(d)
RETURN d, collect(DISTINCT older) AS older, collect(DISTINCT newer) AS newer
