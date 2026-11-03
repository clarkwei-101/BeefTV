package generation

import (
	"context"
	"testing"

	"infinite-canvas/backend/internal/model"
)

// 本机桥接（beeftv_proxy.py）与后端同机，能直接解码内嵌 data URL。
// 历史上这里 RequireURL=true 会让带参考图的本地视频任务直接失败，
// 报「当前模型协议要求公网素材地址，本地工作区请改用支持内嵌素材的模型」。
func TestMediaHydrationPolicyAllowsInlineMediaForLoopbackUpstream(t *testing.T) {
	tests := []struct {
		name    string
		baseURL string
		wantURL bool
	}{
		{name: "ipv4 loopback", baseURL: "http://127.0.0.1:8198", wantURL: false},
		{name: "localhost", baseURL: "http://localhost:8198", wantURL: false},
		{name: "ipv6 loopback", baseURL: "http://[::1]:8198", wantURL: false},
		{name: "other loopback range", baseURL: "http://127.0.0.5:8198", wantURL: false},
		{name: "public host keeps requirement", baseURL: "https://api.example.com", wantURL: true},
		{name: "private lan host keeps requirement", baseURL: "http://192.168.1.193:8188", wantURL: true},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			input := Input{Config: Config{
				InterfaceType: string(model.ChannelInterfaceNewAPIChannel2),
				BaseURL:       test.baseURL,
				Model:         "minimax-h3",
			}}
			policy := MediaHydrationPolicyFor(context.Background(), input)
			if policy.RequireURL != test.wantURL {
				t.Fatalf("RequireURL = %v, want %v (policy %+v)", policy.RequireURL, test.wantURL, policy)
			}
		})
	}
}

// 掩码路径始终走字节路径，不受上游地址形态影响。
func TestMediaHydrationPolicyKeepsMaskOnBytePath(t *testing.T) {
	input := Input{
		Config: Config{
			InterfaceType: string(model.ChannelInterfaceNewAPIChannel2),
			BaseURL:       "http://127.0.0.1:8198",
			Model:         "minimax-h3",
		},
		Mask: &Media{},
	}
	policy := MediaHydrationPolicyFor(context.Background(), input)
	if policy.RequireURL {
		t.Fatalf("mask request must not require public URLs: %+v", policy)
	}
}

// 公网渠道仍必须拿到公网素材地址，放行规则不能扩大到外部上游。
func TestMediaHydrationPolicyKeepsPublicRequirementForRemoteChannels(t *testing.T) {
	for _, interfaceType := range []string{
		string(model.ChannelInterfaceNewAPIVideo),
		string(model.ChannelInterfaceNewAPIChannel2),
		string(model.ChannelInterfaceMiniMaxVideo),
	} {
		input := Input{Config: Config{
			InterfaceType: interfaceType,
			BaseURL:       "https://api.example.com",
			Model:         "seedance",
		}}
		policy := MediaHydrationPolicyFor(context.Background(), input)
		if !policy.RequireURL {
			t.Fatalf("%s must keep RequireURL for public upstream", interfaceType)
		}
	}
}

func TestIsLoopbackUpstream(t *testing.T) {
	tests := []struct {
		baseURL string
		want    bool
	}{
		{baseURL: "http://127.0.0.1:8198", want: true},
		{baseURL: "http://127.0.0.1", want: true},
		{baseURL: "http://localhost:8198", want: true},
		{baseURL: "http://LOCALHOST:8198", want: true},
		{baseURL: "http://[::1]:8198", want: true},
		{baseURL: "https://127.0.0.1:8198", want: true},
		{baseURL: "http://192.168.1.193:8188", want: false},
		{baseURL: "http://10.0.0.5:8198", want: false},
		{baseURL: "https://api.example.com", want: false},
		{baseURL: "http://127.0.0.1.example.com", want: false},
		{baseURL: "", want: false},
		{baseURL: "not a url", want: false},
	}
	for _, test := range tests {
		if got := IsLoopbackUpstream(test.baseURL); got != test.want {
			t.Fatalf("IsLoopbackUpstream(%q) = %v, want %v", test.baseURL, got, test.want)
		}
	}
}
