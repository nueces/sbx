package capability

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	KeySize  = 32
	Lifetime = 30 * time.Second
)

type Metadata struct {
	VMID, Incarnation, FlowID, RuleID, Host string
	Port                                    uint16
	Issued, Expires                         time.Time
}

type Issuer struct {
	key         []byte
	vmID        string
	incarnation string
	now         func() time.Time
	mu          sync.Mutex
	used        map[string]time.Time
}

func GenerateKey() ([]byte, error) {
	key := make([]byte, KeySize)
	if _, err := rand.Read(key); err != nil {
		return nil, err
	}
	return key, nil
}

func ReadKey(r io.Reader) ([]byte, error) {
	key, err := io.ReadAll(io.LimitReader(r, KeySize+1))
	if err != nil || len(key) != KeySize {
		return nil, errors.New("invalid capability key descriptor")
	}
	return key, nil
}

func New(key []byte, vmID, incarnation string) (*Issuer, error) {
	if len(key) != KeySize {
		return nil, fmt.Errorf("capability key must contain exactly %d bytes", KeySize)
	}
	if !validField(vmID) || !validField(incarnation) {
		return nil, errors.New("invalid capability identity")
	}
	return &Issuer{key: append([]byte(nil), key...), vmID: vmID, incarnation: incarnation, now: time.Now, used: make(map[string]time.Time)}, nil
}

func (i *Issuer) Issue(meta Metadata) (string, string, error) {
	now := i.now()
	if meta.Issued.IsZero() {
		meta.Issued = now
	}
	if meta.Expires.IsZero() {
		meta.Expires = meta.Issued.Add(Lifetime)
	}
	if meta.Issued.After(now.Add(2*time.Second)) || !meta.Expires.After(meta.Issued) || meta.Expires.Sub(meta.Issued) > Lifetime || !meta.Expires.After(now) {
		return "", "", errors.New("invalid capability lifetime")
	}
	if meta.VMID != i.vmID || meta.Incarnation != i.incarnation || !validField(meta.FlowID) || !validField(meta.RuleID) || meta.Port == 0 {
		return "", "", errors.New("invalid capability metadata")
	}
	host := strings.ToLower(strings.TrimSuffix(meta.Host, "."))
	if !validField(host) || len(host) > 255 {
		return "", "", errors.New("invalid capability destination")
	}
	var nonce [12]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return "", "", err
	}
	fields := []string{
		"2", meta.FlowID, meta.RuleID,
		strconv.FormatInt(meta.Issued.Unix(), 10), strconv.FormatInt(meta.Expires.Unix(), 10),
		base64.RawURLEncoding.EncodeToString(nonce[:]), strconv.Itoa(int(meta.Port)),
	}
	payload := strings.Join(fields, "|")
	password := payload + "|" + i.mac(host, payload)
	if len(password) > 255 {
		return "", "", errors.New("capability password exceeds RFC 1929 limit")
	}
	return host, password, nil
}

func (i *Issuer) Verify(username, password, host string, port uint16) (Metadata, error) {
	parts := strings.Split(password, "|")
	if len(parts) != 8 || parts[0] != "2" || len(username) > 255 || len(password) > 255 || !validField(parts[1]) || !validField(parts[2]) {
		return Metadata{}, errors.New("invalid capability")
	}
	issuedUnix, issuedErr := strconv.ParseInt(parts[3], 10, 64)
	expiresUnix, expiresErr := strconv.ParseInt(parts[4], 10, 64)
	nonce, nonceErr := base64.RawURLEncoding.DecodeString(parts[5])
	claimedPort, portErr := strconv.ParseUint(parts[6], 10, 16)
	normalizedHost := strings.ToLower(strings.TrimSuffix(host, "."))
	if issuedErr != nil || expiresErr != nil || nonceErr != nil || len(nonce) != 12 || portErr != nil || !validField(normalizedHost) || username != normalizedHost || uint16(claimedPort) != port {
		return Metadata{}, errors.New("invalid capability destination")
	}
	if !hmac.Equal([]byte(i.mac(username, strings.Join(parts[:7], "|"))), []byte(parts[7])) {
		return Metadata{}, errors.New("invalid capability")
	}
	now := i.now()
	issued, expiry := time.Unix(issuedUnix, 0), time.Unix(expiresUnix, 0)
	if issued.After(now.Add(2*time.Second)) || !expiry.After(issued) || !expiry.After(now) || expiry.Sub(issued) > Lifetime {
		return Metadata{}, errors.New("expired capability")
	}
	i.mu.Lock()
	defer i.mu.Unlock()
	for token, expiry := range i.used {
		if !expiry.After(now) {
			delete(i.used, token)
		}
	}
	if _, replay := i.used[password]; replay {
		return Metadata{}, errors.New("replayed capability")
	}
	i.used[password] = expiry
	return Metadata{VMID: i.vmID, Incarnation: i.incarnation, FlowID: parts[1], RuleID: parts[2], Host: normalizedHost, Port: port, Issued: issued, Expires: expiry}, nil
}

func validField(value string) bool {
	if value == "" || strings.Contains(value, "|") {
		return false
	}
	for _, ch := range value {
		if ch < 33 || ch >= 127 {
			return false
		}
	}
	return true
}

func (i *Issuer) mac(host, payload string) string {
	mac := hmac.New(sha256.New, i.key)
	fmt.Fprintf(mac, "%s|%s|%s|%s", i.vmID, i.incarnation, host, payload)
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}
