// http_srv serves /var/lib/postgresql/data on :18888. deploy-patched-binaries.sh
// runs it inside postgres-0 so the ate-api Deployment and the atelet DaemonSet
// can download the patched binaries (bin_ateapi.gz, bin_atelet.gz) at pod start.
//
// SECURITY: this serves the WHOLE Postgres data directory, including the raw
// database files under pgdata/, unauthenticated, to anything that can reach the
// pod IP. It was a demo shortcut. For anything beyond a throwaway demo cluster,
// bake the binaries into images in Artifact Registry instead.
//
// Build (static, no module needed): CGO_ENABLED=0 go build -ldflags="-s -w" -o http_srv http_srv.go
package main

import "net/http"

func main() { http.ListenAndServe(":18888", http.FileServer(http.Dir("/var/lib/postgresql/data"))) }
