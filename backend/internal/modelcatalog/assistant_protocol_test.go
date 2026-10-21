package modelcatalog

import "testing"

// 本机部署脚本（start.sh / deploy/scripts/start-public.sh）与迁移工具长期把
// 文本模型的协议写成通用写法 "openai"。助手会话必须能解析它，否则
// /assistant/status 只会返回 model_protocol_unsupported，内置创作助手永远不可用。
func TestChannelModelProtocolAcceptsLegacyOpenAISpellingForText(t *testing.T) {
	channel := AssistantChannel{
		ID: "mac-ollama-local",
		ModelProfiles: []AssistantModelProfile{
			{Model: "qwen3:8b", Capability: "text", Protocol: "openai"},
		},
	}

	protocol, ok := ChannelModelProtocol(channel, "qwen3:8b")
	if !ok {
		t.Fatal("legacy openai protocol must resolve for a text model")
	}
	if protocol != "chat-completion" {
		t.Fatalf("protocol = %q, want chat-completion", protocol)
	}
}

// 归一化只发生在文本能力上。图片/视频/音频模型即使声明 "openai"，
// 也不能被助手会话当成可用文本协议。
func TestChannelModelProtocolRejectsNonTextCapabilities(t *testing.T) {
	for _, capability := range []string{"image", "video", "audio"} {
		channel := AssistantChannel{
			ID: "dgx-local",
			ModelProfiles: []AssistantModelProfile{
				{Model: "m-" + capability, Capability: capability, Protocol: "openai"},
			},
		}
		if _, ok := ChannelModelProtocol(channel, "m-"+capability); ok {
			t.Fatalf("%s model must not resolve as an assistant text protocol", capability)
		}
	}
}

// 真正不受支持的协议仍然必须被拒绝，不能因为新增了 "openai" 而放宽。
func TestChannelModelProtocolStillRejectsUnknownProtocol(t *testing.T) {
	channel := AssistantChannel{
		ID: "weird",
		ModelProfiles: []AssistantModelProfile{
			{Model: "m", Capability: "text", Protocol: "totally-unknown"},
		},
	}
	protocol, ok := ChannelModelProtocol(channel, "m")
	if !ok {
		t.Fatal("model must be found")
	}
	if protocol != "" {
		t.Fatalf("protocol = %q, want empty so the caller reports protocol unsupported", protocol)
	}
}

// 端到端：本地文本渠道声明 "openai" 时，助手供应商必须解析成功，
// 而不是返回 model_protocol_unsupported。
func TestResolveAssistantProviderAcceptsLegacyOpenAIProtocol(t *testing.T) {
	snapshot := AssistantConfigSnapshot{
		AssistantModel: "mac-ollama-local::qwen3:8b",
		Channels: []AssistantChannel{{
			ID: "mac-ollama-local", Name: "Qwen3 8B", Enabled: true,
			BaseURL: "http://127.0.0.1:8198", APIKey: "local",
			Models:   []string{"qwen3:8b"},
			ModelProfiles: []AssistantModelProfile{
				{Model: "qwen3:8b", Capability: "text", Protocol: "openai"},
			},
		}},
	}

	provider, err := ResolveAssistantProvider(snapshot, nil)
	if err != nil {
		t.Fatalf("assistant provider must resolve: %v", err)
	}
	if provider.Protocol != "chat-completion" {
		t.Fatalf("protocol = %q, want chat-completion", provider.Protocol)
	}
	if provider.Model != "qwen3:8b" || provider.ChannelID != "mac-ollama-local" {
		t.Fatalf("unexpected provider: %#v", provider)
	}
}
