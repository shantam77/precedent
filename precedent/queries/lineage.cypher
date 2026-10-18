// The supersede chain around one decision: what it replaced (older) and what replaced it (newer), up to 10 steps each way.
MATCH (d:Decision {id: $id})
OPTIONAL MATCH back = (d)-[:SUPERSEDES*1..10]->(older:Decision)
OPTIONAL MATCH fwd  = (newer:Decision)-[:SUPERSEDES*1..10]->(d)
RETURN d, collect(DISTINCT older) AS older, collect(DISTINCT newer) AS newer
