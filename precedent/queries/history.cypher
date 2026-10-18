// The graph's own change log: every write since $since, newest first, without the changes made by my own session.
// collect() keeps one row per change even when it touches several decisions.
MATCH (s:Session)-[:MADE]->(ch:Change)
WHERE ch.at>= $since AND s.id <> $exclude
OPTIONAL MATCH (ch)-[:ON]->(d:Decision)
RETURN ch.id as ID ,  ch.at as AT , ch.op as OP , ch.actor as Actor , ch.harness as HARNESS , ch.reason as reason, collect(d.id) as decision_id
ORDER BY ch.at DESC LIMIT $limit
