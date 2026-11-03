package main

import (
	"crypto/aes"
	"crypto/cipher"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"strings"
)

func main() {
	connPath := os.Args[1]
	keyPath := os.Args[2]
	raw, err := os.ReadFile(connPath)
	if err != nil {
		panic(err)
	}
	var conn map[string]any
	if err := json.Unmarshal(raw, &conn); err != nil {
		panic(err)
	}
	key, err := os.ReadFile(keyPath)
	if err != nil {
		panic(err)
	}
	enc, _ := conn["encryptedApiKey"].(string)
	if !strings.HasPrefix(enc, "enc:v1:") {
		fmt.Println("not encrypted:", enc)
		return
	}
	payload, err := base64.RawStdEncoding.DecodeString(strings.TrimPrefix(enc, "enc:v1:"))
	if err != nil {
		panic(err)
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		panic(err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		panic(err)
	}
	nonce := payload[:gcm.NonceSize()]
	ct := payload[gcm.NonceSize():]
	pt, err := gcm.Open(nil, nonce, ct, nil)
	if err != nil {
		panic(err)
	}
	fmt.Print(string(pt))
}
