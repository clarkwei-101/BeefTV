package generation

import (
	"context"
	"strings"

	"infinite-canvas/backend/internal/model"
	"infinite-canvas/backend/internal/providerpreset"
)

func MediaHydrationPolicyFor(ctx context.Context, input Input) MediaHydrationPolicy {
	policy := MediaHydrationPolicy{PreferURL: PrefersMediaURLs(input.Config.InterfaceType, input)}
	if model.IsVolcengineArkVideoProtocol(model.ChannelInterfaceType(input.Config.InterfaceType)) {
		return MediaHydrationPolicy{PreferHTTPS: true}
	}
	if IsBeefAPIVideoConfig(ctx, input.Config) {
		if contract, ok := providerpreset.BeefAPIVideoContract(input.Config.Model); ok && contract.InlineMedia && (contract.Protocol == input.Config.InterfaceType || IsSeedanceVideoConfig(input.Config)) {
			return MediaHydrationPolicy{PreferHTTPS: true, KeepLocal: IsBeefAPISeedancePreuploadConfig(ctx, input.Config)}
		}
	}
	if strings.TrimSpace(input.Config.InterfaceType) == string(model.ChannelInterfaceNewAPIChannel1) {
		policy.RequireURL = false
		policy.PreferURL = false
		return policy
	}
	switch strings.TrimSpace(input.Config.InterfaceType) {
	case string(model.ChannelInterfaceNewAPIVideo), string(model.ChannelInterfaceNewAPIChannel1), string(model.ChannelInterfaceNewAPIChannel2), string(model.ChannelInterfaceVolcengineArkVideo), string(model.ChannelInterfaceVolcengineArkAgentPlanVideo), string(model.ChannelInterfaceMiniMaxVideo):
		policy.RequireURL = true
		policy.PreferURL = true
	}
	if adapter, ok := ProtocolAdapterForContext(ctx, input.Config.InterfaceType); ok {
		policy.RequireURL = adapter.Metadata().RequiresPublicMediaURLs
		policy.PreferURL = policy.PreferURL || policy.RequireURL
	}
	if input.Mask != nil {
		policy.RequireURL = false
		policy.PreferURL = false
	}
	// 本机桥接（loopback 上游）能直接消费内嵌素材：它与后端同机，data URL
	// 到桥接进程内再解码即可，不需要公网可达的素材地址。公网渠道保持原规则。
	if policy.RequireURL && IsLoopbackUpstream(input.Config.BaseURL) {
		policy.RequireURL = false
		policy.PreferURL = false
	}
	return policy
}

// PrefersMediaURLs lists protocols that accept remote URLs. Multipart/byte
// protocols keep the byte path so a smaller download cannot change the request.
func PrefersMediaURLs(interfaceType string, input Input) bool {
	if input.Mask != nil {
		return false
	}
	switch strings.TrimSpace(interfaceType) {
	case string(model.ChannelInterfaceChatCompletion), string(model.ChannelInterfaceOpenAIResponse), string(model.ChannelInterfaceClaudeAPI),
		string(model.ChannelInterfaceGrokImage), string(model.ChannelInterfaceVolcengineArkImage), string(model.ChannelInterfaceVolcengineArkAgentPlanImage),
		string(model.ChannelInterfaceXAIVideo), string(model.ChannelInterfaceNovitaVideo),
		string(model.ChannelInterfaceMiniMaxVideo), string(model.ChannelInterfaceNewAPIVideo),
		string(model.ChannelInterfaceNewAPIChannel1), string(model.ChannelInterfaceNewAPIChannel2),
		string(model.ChannelInterfaceVolcengineArkVideo), string(model.ChannelInterfaceVolcengineArkAgentPlanVideo):
		return true
	}
	if IsGrokVideoConfig(input.Config) || IsSeedanceVideoConfig(input.Config) || IsArkPlanVideoConfig(input.Config) {
		return true
	}
	return false
}
