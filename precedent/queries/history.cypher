// Show what changed in the graph since a given time, newest first.
// Changes from my own session are left out, and each change stays one row even if it touched several decisions.
MATCH (s:Session)-[:MADE]->(ch:Change)
WHERE ch.at>= $since AND s.id <> $exclude
OPTIONAL MATCH (ch)-[:ON]->(d:Decision)
RETURN ch.id as ID ,  ch.at as AT , ch.op as OP , ch.actor as Actor , ch.harness as HARNESS , ch.reason as reason, collect(d.id) as decision_id
ORDER BY ch.at DESC LIMIT $limit
