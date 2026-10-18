// Find which part of the codebase a file belongs to.
// A brand new file won't be in the graph yet, so Python falls back to the closest folder.
MATCH (f:File {path: $path})-[i:IN]->(c:Component)

return c.name as name
