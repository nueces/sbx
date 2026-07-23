package capability

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"sync"
	"time"
)

const Lifetime = 30 * time.Second

type Metadata struct {
	VMID, Incarnation, FlowID, RuleID, Host string
	Port                                    uint16
	Expires                                 time.Time
}

type Issuer struct {
	key  []byte
	now  func() time.Time
	mu   sync.Mutex
	used map[string]struct{}
}

func New(key []byte) (*Issuer, error) {
	if len(key) < 32 {
		return nil, errors.New("capability key must contain at least 32 bytes")
	}
	return &Issuer{key: append([]byte(nil), key...), now: time.Now, used: make(map[string]struct{})}, nil
}

func (i *Issuer) Issue(meta Metadata) (string, string, error) {
	if meta.Expires.IsZero() {
		meta.Expires = i.now().Add(Lifetime)
	}
	if meta.Expires.After(i.now().Add(Lifetime)) || !meta.Expires.After(i.now()) {
		return "", "", errors.New("invalid capability expiry")
	}
	var nonce [12]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return "", "", err
	}
	fields := []string{"1", meta.VMID, meta.Incarnation, meta.FlowID, meta.RuleID, strconv.FormatInt(meta.Expires.Unix(), 10), base64.RawURLEncoding.EncodeToString(nonce[:])}
	for _, value := range fields[1:5] {
		if value == "" || strings.Contains(value, "|") {
			return "", "", errors.New("invalid capability metadata")
		}
	}
	username := strings.Join(fields, "|")
	if len(username) > 255 {
		return "", "", errors.New("capability username exceeds RFC 1929 limit")
	}
	return username, i.mac(username, meta.Host, meta.Port), nil
}

func (i *Issuer) Verify(username, password, host string, port uint16) (Metadata, error) {
	parts := strings.Split(username, "|")
	if len(parts) != 7 || parts[0] != "1" || len(username) > 255 {
		return Metadata{}, errors.New("invalid capability")
	}
	expires, err := strconv.ParseInt(parts[5], 10, 64)
	if err != nil {
		return Metadata{}, errors.New("invalid capability")
	}
	if _, err := base64.RawURLEncoding.DecodeString(parts[6]); err != nil {
		return Metadata{}, errors.New("invalid capability")
	}
	expected := i.mac(username, host, port)
	if !hmac.Equal([]byte(expected), []byte(password)) {
		return Metadata{}, errors.New("invalid capability")
	}
	now := i.now()
	expiry := time.Unix(expires, 0)
	if !expiry.After(now) || expiry.After(now.Add(Lifetime+2*time.Second)) {
		return Metadata{}, errors.New("expired capability")
	}
	i.mu.Lock()
	defer i.mu.Unlock()
	for token := range i.used {
		fields := strings.Split(token, "|")
		old, _ := strconv.ParseInt(fields[5], 10, 64)
		if old < now.Unix() {
			delete(i.used, token)
		}
	}
	if _, replay := i.used[username]; replay {
		return Metadata{}, errors.New("replayed capability")
	}
	i.used[username] = struct{}{}
	return Metadata{VMID: parts[1], Incarnation: parts[2], FlowID: parts[3], RuleID: parts[4], Host: strings.ToLower(strings.TrimSuffix(host, ".")), Port: port, Expires: expiry}, nil
}

func (i *Issuer) mac(username, host string, port uint16) string {
	mac := hmac.New(sha256.New, i.key)
	fmt.Fprintf(mac, "%s\x00%s\x00%d", username, strings.ToLower(strings.TrimSuffix(host, ".")), port)
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}
