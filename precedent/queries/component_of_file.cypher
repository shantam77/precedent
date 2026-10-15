// Which component a file belongs to (files created after the scan fall back to the longest folder prefix in Python).
MATCH (f:File {path: $path})-[:IN]->(c:Component) RETURN c.name
