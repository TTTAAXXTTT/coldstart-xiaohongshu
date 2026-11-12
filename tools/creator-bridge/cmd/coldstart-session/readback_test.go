package main

import "testing"

func TestPublicReadRequestTargets(t *testing.T) {
    feed := "1234567890abcdef12345678"
    cases := []struct {feed, comment, token string; ok bool}{
        {feed,"","fixture-token",true},
        {feed,feed,"fixture-token",true},
        {"../outside",feed,"fixture-token",false},
        {feed,"bad-id","fixture-token",false},
        {feed,"","",false},
    }
    for _, c := range cases {
        if validCommentRequest(c.feed,c.comment,c.token)!=c.ok {t.Fatalf("request target validation mismatch")}
    }
}
