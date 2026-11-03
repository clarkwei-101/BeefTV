package modelcatalog

import (
	"testing"

	"infinite-canvas/backend/internal/model"
)

// 前台模型模式（FeatureFrontendModels 开启）下，本地模型 ID 必须走
// resolveLocalModelSelection 拿代理端点，而不是丢给只认数据库逻辑模型的
// ResolveLogical —— 否则本地模型会被判成「所选模型不可用」。
func TestSelectTaskModelRoutesLocalModelInFrontendMode(t *testing.T) {
	input := map[string]any{
		"mode":   "video",
		"prompt": "a red square drifting",
		"config": map[string]any{"model": "minimax-h3"},
	}
	resolvedCalled := false
	lookup := TaskSelectLookup{
		ResolveLogical: func(logicalModelID string, intent ModelRequestIntent) (*RoutedModel, error) {
			resolvedCalled = true
			return nil, errModelUnavailable
		},
	}
	result, err := SelectTaskModel(TaskSelectRequest{
		Input:           input,
		LogicalModelID:  "minimax-h3-local::minimax-h3",
		Type:            "canvas_video",
		FrontendEnabled: true,
	}, lookup)
	if err != nil {
		t.Fatalf("local model rejected in frontend mode: %v", err)
	}
	if resolvedCalled {
		t.Fatalf("local model must not be routed through the logical model catalog")
	}
	config, ok := result.Input["config"].(map[string]any)
	if !ok {
		t.Fatalf("config missing: %#v", result.Input)
	}
	if got, _ := config["interfaceType"].(string); got != string(model.ChannelInterfaceNewAPIChannel2) {
		t.Fatalf("interfaceType = %q, want newapi-channel-2", got)
	}
	if got, _ := config["baseUrl"].(string); got == "" {
		t.Fatalf("baseUrl not hydrated: %#v", config)
	}
	if got, _ := config["model"].(string); got != "minimax-h3" {
		t.Fatalf("model = %q, want minimax-h3", got)
	}
}

type modelLookupError struct{}

func (modelLookupError) Error() string { return "所选模型不可用" }

var errModelUnavailable = modelLookupError{}
