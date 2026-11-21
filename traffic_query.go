package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/sagernet/sing-box/experimental/v2rayapi"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func readStats(address string) (map[string][2]int64, error) {
	conn, err := grpc.NewClient(address, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, err
	}
	defer conn.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	request := &v2rayapi.QueryStatsRequest{
		Patterns: []string{"user>>>"},
	}
	// sing-box registers the V2Ray-compatible service name at runtime.
	var reply v2rayapi.QueryStatsResponse
	err = conn.Invoke(ctx, "/v2ray.core.app.stats.command.StatsService/QueryStats", request, &reply)
	if err != nil {
		return nil, err
	}
	totals := make(map[string][2]int64)
	for _, counter := range reply.GetStat() {
		parts := strings.Split(counter.GetName(), ">>>")
		if len(parts) != 4 || parts[0] != "user" || parts[2] != "traffic" {
			continue
		}
		usage := totals[parts[1]]
		switch parts[3] {
		case "uplink":
			usage[0] = counter.GetValue()
		case "downlink":
			usage[1] = counter.GetValue()
		default:
			continue
		}
		totals[parts[1]] = usage
	}
	return totals, nil
}

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: yhl-traffic address")
		os.Exit(2)
	}
	totals, err := readStats(os.Args[1])
	if err == nil {
		err = json.NewEncoder(os.Stdout).Encode(totals)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
