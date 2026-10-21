package modelcatalog

import (
	"fmt"
	"os"
	"strings"

	"infinite-canvas/backend/internal/beefapi"
	"infinite-canvas/backend/internal/kernel"
	"infinite-canvas/backend/internal/model"
)

// TaskSelectRequest is the typed admission input. App supplies the live
// frontend-models flag; this package owns routing, system-channel rebuild,
// variant rewrite, and spec defaults.
type TaskSelectRequest struct {
	Input           map[string]any
	LogicalModelID  string
	Type            string
	Operation       string
	FrontendEnabled bool
}

// TaskSelectLookup is the persistence/router port. Callers pass repo and the
// already-owned logical-model resolver; they must not inject Service methods
// that still contain admission policy.
type TaskSelectLookup struct {
	SystemChannel     func(id string) (*model.ModelChannel, error)
	ChannelModelByKey func(channelID, modelKey string) (*model.ChannelModel, error)
	ResolveLogical    func(logicalModelID string, intent ModelRequestIntent) (*RoutedModel, error)
}

// TaskSelectResult is the admitted input plus an optional frontend route binding.
type TaskSelectResult struct {
	Input  map[string]any
	Routed *RoutedModel
}

// SelectTaskModel is the production admission algorithm for ordinary generation.
// Workflow-plugin authorization stays a cross-domain bridge in the caller.
func SelectTaskModel(req TaskSelectRequest, lookup TaskSelectLookup) (TaskSelectResult, error) {
	input := req.Input
	customChannelTask := TaskInputUsesCustomChannel(input)
	if req.FrontendEnabled && !TaskInputUsesSystemChannel(input) && !customChannelTask {
		if strings.TrimSpace(req.LogicalModelID) == "" {
			return TaskSelectResult{}, InvalidModelSelection("前台模型模式下必须指定 logicalModelId")
		}
		if lookup.ResolveLogical == nil {
			return TaskSelectResult{}, InvalidModelSelection("前台模型模式下必须指定 logicalModelId")
		}
		intent := ModelRequestIntentFromTaskInput(input, req.Type, req.Operation)
		routed, err := lookup.ResolveLogical(req.LogicalModelID, intent)
		if err != nil {
			return TaskSelectResult{}, err
		}
		return TaskSelectResult{Input: ApplyRoutedProviderSelection(input, routed), Routed: routed}, nil
	}

	if strings.TrimSpace(req.LogicalModelID) != "" {
		// Local model IDs (e.g. "dgx-flux-local::flux-schnell") bypass the system
		// channel catalog: hydrate the config with the proxy endpoint and return
		// so execution can proceed with the raw config from the frontend.
		if localResult, ok := resolveLocalModelSelection(req.LogicalModelID, input, req.Type, req.Operation); ok {
			return TaskSelectResult{Input: localResult}, nil
		}
		// Only reject if it was a hosted-style logical model ID that genuinely
		// failed the snapshot lookup — not for the local-model format.
		return TaskSelectResult{}, ModelCatalogMismatch("模型目录已更新，请重新选择")
	}
	if !customChannelTask {
		resolved, err := ResolveSystemChannelModelSelection(input, req.Type, req.Operation, lookup)
		if err != nil {
			return TaskSelectResult{}, err
		}
		return TaskSelectResult{Input: resolved}, nil
	}
	return TaskSelectResult{Input: input}, nil
}

// ResolveSystemChannelModelSelection rebuilds protocol, capability, execution
// spec, and upstream model identity from the server record. Client workflow
// interfaceType, variantId, and providerModelKey cannot drive execution.
func ResolveSystemChannelModelSelection(input map[string]any, taskType string, operation string, lookup TaskSelectLookup) (map[string]any, error) {
	config, ok := input["config"].(map[string]any)
	if !ok {
		// Config is missing. If a LogicalModelID was provided (via task metadata)
		// this is a local model that was already handled upstream. Return the input
		// unchanged so execution proceeds with the raw config from the frontend.
		return input, nil
	}

	channelID := strings.TrimSpace(admissionString(config["channelId"]))
	modelKey := strings.TrimPrefix(strings.TrimSpace(admissionString(config["model"])), "models/")

	if channelID == "" || modelKey == "" {
		return input, InvalidModelSelection("必须指定系统渠道和模型")
	}
	if lookup.SystemChannel == nil || lookup.ChannelModelByKey == nil {
		return input, InvalidModelSelection("指定的渠道不存在")
	}

	channel, err := lookup.SystemChannel(channelID)
	if err != nil {
		return input, InvalidModelSelection("指定的渠道不存在")
	}
	if !channel.Enabled || channel.Scope != model.ChannelScopeSystem {
		return input, InvalidModelSelection("指定的渠道不可用")
	}

	channelModel, err := lookup.ChannelModelByKey(channelID, modelKey)
	if err != nil {
		return input, InvalidModelSelection("指定的模型不存在")
	}
	if !channelModel.Enabled {
		return input, InvalidModelSelection("指定的模型已停用")
	}
	if channelModel.Protocol == "" {
		return input, InvalidModelSelection("指定的模型未配置请求协议")
	}

	nextConfig := make(map[string]any, len(config)+6)
	for key, value := range config {
		switch key {
		case "channelId", "channelModelKey", "variantId", "providerModelKey", "apiFormat", "interfaceType", "baseUrl", "apiKey", "secretKey", "headers", "model", "capabilityConfig":
			continue
		default:
			nextConfig[key] = value
		}
	}
	if options, ok := input["capabilityOptions"].(map[string]any); ok {
		for key, value := range options {
			canonical := canonicalCapabilityOptionName(key)
			if isCapabilityOptionFor(channelModel.Capability, canonical) {
				nextConfig[canonical] = value
			}
		}
	}

	capabilityConfig, err := normalizedChannelModelCapability(channelModel)
	if err != nil {
		return input, InvalidModelSelection("指定的模型能力配置无效，请联系管理员")
	}
	var capabilitySpec *CapabilitySpec
	if normalizeCapability(channelModel.Capability) != "audio" {
		spec, specErr := CapabilitySpecFromModelCapabilityConfig(capabilityConfig, channelModel.Capability)
		if specErr != nil {
			return input, InvalidModelSelection("指定的模型能力配置无效，请联系管理员")
		}
		capabilitySpec = &spec
	}
	ApplyChannelCapabilityDefaults(nextConfig, channelModel.Capability, capabilityConfig)
	input["config"] = nextConfig
	var declaredOptions map[string]OptionConstraint
	if capabilitySpec != nil {
		declaredOptions = capabilitySpec.Options
	}
	input["capabilityOptions"] = CapabilityOptionsFromConfig(channelModel.Capability, nextConfig, declaredOptions)

	intent := ModelRequestIntentFromTaskInput(input, taskType, operation)
	if normalizeCapability(intent.Capability) != normalizeCapability(channelModel.Capability) {
		return input, ModelCapabilityNotSupported("所选模型与任务能力不匹配")
	}
	if normalizeCapability(channelModel.Capability) != "audio" {
		if capabilitySpec == nil {
			return input, InvalidModelSelection("指定的模型能力配置无效，请联系管理员")
		}
		if match := MatchCapability(*capabilitySpec, intent); !match.Matched {
			return input, ModelCapabilityNotSupported("所选模型不支持当前请求：" + strings.Join(match.Reasons, "；"))
		}
	}

	variantIntent := intent
	if normalizeCapability(channelModel.Capability) == "image" {
		variantOptions := make(map[string]any, len(intent.Options)+2)
		for k, v := range intent.Options {
			variantOptions[k] = v
		}
		rawQuality := strings.ToLower(strings.TrimSpace(fmt.Sprint(nextConfig["quality"])))
		if rawQuality != "" && rawQuality != "<nil>" && rawQuality != "auto" && rawQuality != "any" {
			variantOptions["quality"] = rawQuality
		} else if variantOptions["quality"] == nil || variantOptions["quality"] == "" || variantOptions["quality"] == "auto" {
			variantOptions["quality"] = "1k"
		}
		if rawSize := strings.ToLower(strings.TrimSpace(fmt.Sprint(nextConfig["size"]))); rawSize != "" && rawSize != "<nil>" && rawSize != "auto" {
			variantOptions["size"] = rawSize
		}
		variantIntent.Options = variantOptions
	}

	variant := channelModelVariantForIntent(*channelModel, variantIntent)
	if variant == nil {
		variant = channelModelVariantForIntent(*channelModel, intent)
	}
	if len(channelModel.Variants) > 0 && variant == nil {
		return input, ModelCapabilityNotSupported("指定的模型不支持当前规格")
	}

	nextConfig["channelId"] = channel.ID
	nextConfig["model"] = channelModel.ModelKey
	nextConfig["channelModelKey"] = channelModel.ModelKey
	if variant != nil {
		nextConfig["variantId"] = variant.ID
		nextConfig["providerModelKey"] = kernel.FirstNonEmpty(variant.ProviderModelKey, channelModel.ProviderModelKey, channelModel.ModelKey)
	} else {
		delete(nextConfig, "variantId")
		nextConfig["providerModelKey"] = kernel.FirstNonEmpty(channelModel.ProviderModelKey, channelModel.ModelKey)
	}
	nextConfig["interfaceType"] = string(channelModel.Protocol)
	nextConfig["apiFormat"] = ChannelAPIFormatForProtocol(channel.APIFormat, channelModel.Protocol)
	return input, nil
}

// ApplyChannelCapabilityDefaults only fills parameters the client left blank,
// using administrator-saved defaults. Values are later copied into
// capabilityOptions so match, SKU, and provider execution see one spec.
func ApplyChannelCapabilityDefaults(config map[string]any, capability string, profile *ModelCapabilityConfig) {
	setDefault := func(key string, value any) {
		if existing, exists := config[key]; !exists || existing == nil || strings.TrimSpace(fmt.Sprint(existing)) == "" {
			config[key] = fmt.Sprint(value)
		}
	}
	switch normalizeCapability(capability) {
	case "image":
		if profile == nil || profile.Image == nil {
			return
		}
		if profile.Image.Size.Parameter != "none" {
			setDefault("size", profile.Image.Size.Default)
		}
		if profile.Image.Quality.Supported {
			setDefault("quality", profile.Image.Quality.Default)
		}
		setDefault("transparentBackground", profile.Image.TransparentBackground.Default)
		setDefault("count", 1)
	case "video":
		if profile == nil || profile.Video == nil {
			return
		}
		if videoDurationSupported(profile.Video) {
			setDefault("videoSeconds", profile.Video.Duration.Default)
		}
		setDefault("size", profile.Video.DefaultRatio)
		setDefault("vquality", profile.Video.DefaultResolution)
		setDefault("videoGenerateAudio", profile.Video.GenerateAudio.Default)
		setDefault("videoWatermark", profile.Video.Watermark.Default)
	}
}

func CapabilityOptionsFromConfig(capability string, config map[string]any, declared map[string]OptionConstraint) map[string]any {
	options := map[string]any{}
	for key, value := range config {
		canonical := canonicalCapabilityOptionName(key)
		if !isCapabilityOptionFor(capability, canonical) || value == nil || strings.TrimSpace(fmt.Sprint(value)) == "" {
			continue
		}
		if declared != nil {
			if _, ok := declared[canonical]; !ok {
				continue
			}
		}
		options[canonical] = value
	}
	return options
}

func TaskInputUsesCustomChannel(input map[string]any) bool {
	if TaskInputUsesWorkflowProvider(input) {
		return false
	}
	config, ok := input["config"].(map[string]any)
	if !ok {
		return false
	}
	channelID, _ := config["channelId"].(string)
	baseURL, _ := config["baseUrl"].(string)
	apiKey, _ := config["apiKey"].(string)
	credentialRef, _ := config["credentialRef"].(string)
	if strings.EqualFold(strings.TrimSpace(credentialRef), beefapi.CredentialRef) || strings.TrimSpace(channelID) == beefapi.ChannelID {
		return strings.TrimSpace(baseURL) != "" && strings.TrimSpace(apiKey) != ""
	}
	if strings.TrimSpace(channelID) != "" || SystemChannelIDFromBaseURL(baseURL) != "" {
		return false
	}
	return strings.TrimSpace(baseURL) != "" && strings.TrimSpace(apiKey) != ""
}

func TaskInputUsesSystemChannel(input map[string]any) bool {
	config, ok := input["config"].(map[string]any)
	if !ok {
		return false
	}
	channelID, _ := config["channelId"].(string)
	return strings.TrimSpace(channelID) != ""
}

func TaskInputUsesWorkflowProvider(input map[string]any) bool {
	config, ok := input["config"].(map[string]any)
	if !ok {
		return false
	}
	// 系统渠道的 interfaceType 是客户端缓存，不是授权事实；必须先走系统模型 admission。
	if strings.TrimSpace(admissionString(config["channelId"])) != "" {
		return false
	}
	return IsWorkflowInterfaceType(admissionString(config["interfaceType"]))
}

func IsWorkflowInterfaceType(value string) bool {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case string(model.ChannelInterfaceRunningHubImage), string(model.ChannelInterfaceRunningHubVideo), string(model.ChannelInterfaceRunningHubAudio):
		return true
	default:
		return false
	}
}

func HasExecutableVideoConfig(input map[string]any) bool {
	mode, _ := input["mode"].(string)
	config, ok := input["config"].(map[string]any)
	if mode != "video" || !ok {
		return false
	}
	interfaceType := admissionString(config["interfaceType"])
	if IsWorkflowInterfaceType(interfaceType) {
		if admissionString(config["workflowId"]) == "" && admissionString(config["webappId"]) == "" && admissionString(config["model"]) == "" {
			return false
		}
		return admissionString(config["baseUrl"]) != "" && admissionString(config["apiKey"]) != ""
	}
	if admissionString(config["model"]) == "" {
		return false
	}
	return admissionString(config["channelId"]) != "" || (admissionString(config["baseUrl"]) != "" && admissionString(config["apiKey"]) != "")
}

// ChannelAPIFormatForProtocol chooses auth/envelope format from the model
// protocol. A system channel may host mixed protocols; the channel-level
// APIFormat is only a fallback when protocol is empty.
func ChannelAPIFormatForProtocol(channelDefault string, protocol model.ChannelInterfaceType) string {
	switch protocol {
	case model.ChannelInterfaceGeminiVeo, model.ChannelInterfaceGeminiImage:
		return "gemini"
	case model.ChannelInterfaceClaudeAPI:
		return "claude"
	case "":
		return strings.TrimSpace(channelDefault)
	default:
		return "openai"
	}
}

// resolveLocalModelSelection handles local model IDs in "channelID::modelKey" format.
// It hydrates the config with the proxy base URL so execution can proceed.
// Returns (result, true) when the ID was a local model format and was handled.
// Returns (nil, false) when the ID is not a local model format and normal
// hosted-model processing should continue.
// LocalProxyBaseURLEnv overrides where local channels reach beeftv_proxy.py.
const LocalProxyBaseURLEnv = "CANVAS_LOCAL_PROXY_BASE_URL"

// DefaultLocalProxyBaseURL is an IPv4 literal on purpose: beeftv_proxy.py binds
// 0.0.0.0 only, so "localhost" resolves to ::1 first and every call is refused.
const DefaultLocalProxyBaseURL = "http://127.0.0.1:8198"

// localProxyBaseURL returns the configured local proxy endpoint, falling back to
// the IPv4-safe default. Trailing slashes are trimmed so callers can append paths.
func localProxyBaseURL() string {
	configured := strings.TrimSpace(os.Getenv(LocalProxyBaseURLEnv))
	if configured == "" {
		return DefaultLocalProxyBaseURL
	}
	return strings.TrimRight(configured, "/")
}

// resolveLocalModelSelection hydrates the config with the local proxy endpoint.
// It hydrates the config with the proxy base URL so execution can proceed.
// Returns (result, true) when the ID was a local model format and was handled.
// Returns (nil, false) when the ID is not a local model format and normal
// hosted-model processing should continue.
func resolveLocalModelSelection(logicalModelID string, input map[string]any, taskType string, operation string) (map[string]any, bool) {
	// Only handle IDs that contain "::" — the local model format.
	// Hosted logical model IDs will be handled by the normal route below.
	if !strings.Contains(logicalModelID, "::") {
		return nil, false
	}

	parts := strings.SplitN(logicalModelID, "::", 2)
	if len(parts) != 2 || strings.TrimSpace(parts[0]) == "" || strings.TrimSpace(parts[1]) == "" {
		return nil, false
	}
	channelID := strings.TrimSpace(parts[0])
	modelKey := strings.TrimSpace(parts[1])

	// Determine the proxy base URL and interface type from the channel ID.
	// Local channels all point at the local beeftv_proxy.py.
	//
	// The default must be an IPv4 literal, NOT "localhost": beeftv_proxy.py binds
	// 0.0.0.0 (IPv4 only), while Go's resolver prefers ::1 for "localhost", so a
	// localhost base URL fails with "dial tcp [::1]:8198: connection refused" and
	// every local generation dies at the network layer. Override with
	// CANVAS_LOCAL_PROXY_BASE_URL when the proxy is served elsewhere.
	var baseURL string
	var apiFormat string

	switch channelID {
	case "dgx-flux-local", "dgx-sdxl-local":
		// DGX Spark image generation: FLUX / SDXL via ComfyUI
		baseURL = localProxyBaseURL()
		apiFormat = "openai"
	case "minimax-h3-local", "dgx-minimax-local", "dgx-wan-local":
		// MiniMax H3 / Wan 2.1 text-to-video on DGX Spark ComfyUI
		baseURL = localProxyBaseURL()
		apiFormat = "openai"
	case "dgx-deepseek-local":
		// DeepSeek R1 long-context text on DGX Spark
		baseURL = localProxyBaseURL()
		apiFormat = "openai"
	case "mac-ollama-local":
		// Ollama on Mac Studio for local text
		baseURL = localProxyBaseURL()
		apiFormat = "openai"
	case "mac-piper-local":
		// Piper TTS on Mac Studio for local audio
		baseURL = localProxyBaseURL()
		apiFormat = "openai"
	default:
		// Unknown local channel — pass through and let execution fail with a clear error.
		return nil, false
	}

	// Determine the task capability from the task type.
	var taskCapability string
	switch {
	case strings.HasPrefix(taskType, "canvas_image"):
		taskCapability = "image"
	case strings.HasPrefix(taskType, "canvas_video"):
		taskCapability = "video"
	case strings.HasPrefix(taskType, "canvas_audio"):
		taskCapability = "audio"
	case taskType == "canvas_text":
		taskCapability = "text"
	default:
		taskCapability = "image"
	}

	// If this is a local model of the wrong capability for the task type,
	// still pass through (user will see a provider error at execution time).
	// This is more permissive than hard-rejecting, which would surface a confusing
	// "模型目录已更新" message instead of the real execution error.

	// Build or update the config with the resolved fields.
	// NOTE: Do NOT set "channelId" here — resolveProviderConfig rejects local-mode
	// requests with a non-empty ChannelID ("本地工作区不支持系统渠道配置").
	// We only inject baseUrl + model + interfaceType so the execution path
	// (OpenAI-compatible HTTP call to localhost:8198) works without needing a
	// system channel catalog lookup.
	cfg := map[string]any{}
	if existing, ok := input["config"].(map[string]any); ok {
		for k, v := range existing {
			cfg[k] = v
		}
	}
	cfg["baseUrl"] = baseURL
	cfg["apiFormat"] = apiFormat
	cfg["model"] = modelKey

	// Local proxies (beeftv_proxy.py on this Mac) don't validate the
	// Authorization header — they just route by model — but the executor's
	// validatePreparedInput rejects an empty apiKey with "后端生成任务缺少
	// Base URL、API Key 或模型名". Inject a sentinel so the request reaches
	// the upstream; the proxy ignores the value.
	if strings.TrimSpace(admissionString(cfg["apiKey"])) == "" {
		cfg["apiKey"] = "local-proxy-no-auth"
	}

	// Set interface type based on capability. The local proxy (beeftv_proxy.py)
	// exposes one OpenAI-compatible surface per capability, so we use the
	// capability-appropriate type rather than the upstream channel-specific
	// types like newapi-channel-2.
	switch taskCapability {
	case "image":
		cfg["interfaceType"] = "openai-image"
	case "video":
		// Proxy exposes MiniMax H3 / Wan 2.1 text-to-video through
		// /v1/video/generations (a WIP shim that delegates to the chat
		// completions video path).
		cfg["interfaceType"] = "newapi-channel-2"
	case "audio":
		cfg["interfaceType"] = "openai-audio"
	case "text":
		cfg["interfaceType"] = "chat-completion"
	}

	result := make(map[string]any, len(input))
	for k, v := range input {
		result[k] = v
	}
	result["config"] = cfg

	return result, true
}

func admissionString(value any) string {
	text := strings.TrimSpace(fmt.Sprint(value))
	if text == "<nil>" {
		return ""
	}
	return text
}
