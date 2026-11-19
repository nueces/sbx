package config

import (
	"encoding/json"
	"fmt"
	"os"
	"testing"
)

func TestDirectIPPolicyContract(t *testing.T) {
	data, err := os.ReadFile("../../testdata/direct-ip-cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		Host        string   `json:"host"`
		Mode        string   `json:"mode"`
		AllowRanges []string `json:"allow_ranges"`
		Allowed     bool     `json:"allowed"`
	}
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	protocols := map[string]string{"http-mediated": "http", "tcp-forward": "tcp", "tls-passthrough": "tls"}
	for _, tc := range cases {
		t.Run(fmt.Sprintf("%s-%s-%v", tc.Mode, tc.Host, tc.AllowRanges), func(t *testing.T) {
			cfg := Config{Version: Version, VMID: "vm", Generation: "generation", Quotas: DefaultQuotas(),
				Rules: []Rule{{ID: "target", Protocol: protocols[tc.Mode], Host: tc.Host, Ports: []uint16{443}, AllowRanges: tc.AllowRanges}}}
			input, err := json.Marshal(cfg)
			if err != nil {
				t.Fatal(err)
			}
			_, err = Prepare(input)
			if (err == nil) != tc.Allowed {
				t.Fatalf("allowed=%v, error=%v", tc.Allowed, err)
			}
		})
	}
}
