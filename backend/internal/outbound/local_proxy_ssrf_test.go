package outbound

import (
	"context"
	"testing"
)

// The local generation proxy runs on loopback, so the SSRF guard would
// otherwise block every local model call. The exemption must stay scoped to
// the proxy's exact host:port pair: widening it to the whole host would
// re-open the machine's internal network.
func TestLocalProxyUpstreamAuthorityMatchesExactHostPort(t *testing.T) {
	t.Setenv(LocalProxyBaseURLEnv, "http://127.0.0.1:8198")

	if !LocalProxyUpstreamAuthority("127.0.0.1", "8198") {
		t.Fatal("LocalProxyUpstreamAuthority() should match the configured proxy authority")
	}
	if LocalProxyUpstreamAuthority("127.0.0.1", "8080") {
		t.Fatal("LocalProxyUpstreamAuthority() should not match a different port on the same host")
	}
	if LocalProxyUpstreamAuthority("127.0.0.2", "8198") {
		t.Fatal("LocalProxyUpstreamAuthority() should not match a different host on the same port")
	}
	if LocalProxyUpstreamAuthority("127.0.0.1", "") {
		t.Fatal("LocalProxyUpstreamAuthority() should not match without a port")
	}
}

// Other loopback ports must stay blocked by the SSRF guard.
func TestValidateOutboundURLKeepsBlockingOtherLoopbackPorts(t *testing.T) {
	t.Setenv("CANVAS_ALLOW_PRIVATE_UPSTREAMS", "false")
	t.Setenv("CANVAS_ALLOWED_PRIVATE_UPSTREAM_HOSTS", "")
	t.Setenv(LocalProxyBaseURLEnv, "http://127.0.0.1:8198")

	if _, err := ValidateOutboundURL("http://127.0.0.1:8080"); err == nil {
		t.Fatal("ValidateOutboundURL() should reject loopback ports other than the local proxy")
	}
	if _, err := ValidateOutboundURL("http://127.0.0.1:8198"); err != nil {
		t.Fatalf("ValidateOutboundURL() should allow the local proxy, got %v", err)
	}
}

// The dial-time resolver must accept the proxy authority and reject the rest.
func TestResolveOutboundHostAllowsLocalProxyAuthorityOnly(t *testing.T) {
	t.Setenv("CANVAS_ALLOW_PRIVATE_UPSTREAMS", "false")
	t.Setenv("CANVAS_ALLOWED_PRIVATE_UPSTREAM_HOSTS", "")
	t.Setenv(LocalProxyBaseURLEnv, "http://127.0.0.1:8198")

	if _, err := resolveOutboundHost(context.Background(), "127.0.0.1", "8198"); err != nil {
		t.Fatalf("resolveOutboundHost() should allow the local proxy, got %v", err)
	}
	if _, err := resolveOutboundHost(context.Background(), "127.0.0.1", "8080"); err == nil {
		t.Fatal("resolveOutboundHost() should reject other loopback ports")
	}
}

// The custom relay path keeps its stricter policy: the local proxy exemption
// must not leak into user-supplied custom channels.
func TestValidateCustomRelayURLRejectsLocalProxyWithoutAllowlist(t *testing.T) {
	t.Setenv("CANVAS_ALLOW_PRIVATE_UPSTREAMS", "true")
	t.Setenv("CANVAS_ALLOWED_PRIVATE_UPSTREAM_HOSTS", "")
	t.Setenv(LocalProxyBaseURLEnv, "http://127.0.0.1:8198")

	if _, err := ValidateCustomRelayURL("http://127.0.0.1:8198/v1/models"); err == nil {
		t.Fatal("ValidateCustomRelayURL() should reject HTTP without an exact host allowlist")
	}
}
