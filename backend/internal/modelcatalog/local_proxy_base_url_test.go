package modelcatalog

import "testing"

// Regression: local channels must reach beeftv_proxy.py over IPv4.
//
// beeftv_proxy.py binds 0.0.0.0 (IPv4 only). Go's resolver prefers ::1 for
// "localhost", so a localhost base URL produced
//
//	dial tcp [::1]:8198: connect: connection refused
//
// for EVERY local generation (image, text, audio, video), surfacing only as a
// generic "网络连接失败" / "submission_unknown" task failure.
func TestLocalProxyBaseURLDefaultsToIPv4Literal(t *testing.T) {
	t.Setenv(LocalProxyBaseURLEnv, "")
	base := localProxyBaseURL()
	if base != DefaultLocalProxyBaseURL {
		t.Fatalf("localProxyBaseURL() = %q, want %q", base, DefaultLocalProxyBaseURL)
	}
	if base == "http://localhost:8198" {
		t.Fatal("local proxy base URL must not use localhost: the proxy is IPv4-only")
	}
}

func TestLocalProxyBaseURLHonoursOverride(t *testing.T) {
	t.Setenv(LocalProxyBaseURLEnv, "http://192.168.1.50:9090/")
	if got := localProxyBaseURL(); got != "http://192.168.1.50:9090" {
		t.Fatalf("override not applied or not trimmed: %q", got)
	}
}

func TestLocalProxyBaseURLTrimsWhitespace(t *testing.T) {
	t.Setenv(LocalProxyBaseURLEnv, "   ")
	if got := localProxyBaseURL(); got != DefaultLocalProxyBaseURL {
		t.Fatalf("blank override should fall back to default, got %q", got)
	}
}

// Every local channel must hydrate the same IPv4-reachable endpoint.
func TestResolveLocalModelSelectionUsesReachableBaseURL(t *testing.T) {
	t.Setenv(LocalProxyBaseURLEnv, "")
	channels := []string{
		"dgx-flux-local", "dgx-sdxl-local", "dgx-minimax-local", "minimax-h3-local",
		"dgx-wan-local", "mac-ollama-local", "mac-piper-local",
	}
	for _, channel := range channels {
		t.Run(channel, func(t *testing.T) {
			input := map[string]any{"config": map[string]any{}}
			result, ok := resolveLocalModelSelection(channel+"::model-x", input, "canvas_image", "")
			if !ok {
				t.Fatalf("%s should be recognised as a local channel", channel)
			}
			cfg, _ := result["config"].(map[string]any)
			base, _ := cfg["baseUrl"].(string)
			if base != DefaultLocalProxyBaseURL {
				t.Fatalf("baseUrl = %q, want %q", base, DefaultLocalProxyBaseURL)
			}
		})
	}
}