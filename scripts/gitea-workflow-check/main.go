// Checks Actions workflow files with Gitea's own parser, built from the Gitea source tree it is copied into
// (scripts/gitea-workflow-check.sh does that). It runs the checks DetectWorkflows runs before it creates a run:
// jobparser.ValidateWorkflowStatic, ReadWorkflow and Parse. A file that fails one of them is dropped by that
// Gitea version with only a log warning ("ignore invalid workflow"), so its CI silently stops.
//
// Usage: wfcheck <dir> <expected>, where <dir> holds the <owner>/<repo>/<n>-<path> files written by
// gitea-workflow-check.sh and <expected> is how many it wrote. Every regular file is checked: the exporter has
// already applied Gitea's .yml/.yaml filter, and a count that differs from <expected> fails the run (exit 3),
// so no exported file can go unchecked.
package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"

	"gitea.dev/modules/actions/jobparser"
)

// contentLimit is what Gitea reads of a workflow blob (actions.GetContentFromEntry, util.ReadWithLimit).
const contentLimit = 1024 * 1024

func main() {
	if len(os.Args) != 3 {
		fmt.Println("usage: wfcheck <dir> <expected>")
		os.Exit(2)
	}
	root := os.Args[1]
	expected, err := strconv.Atoi(os.Args[2])
	if err != nil {
		fmt.Println("expected count:", err)
		os.Exit(2)
	}
	bad, total := 0, 0
	err = filepath.Walk(root, func(p string, info os.FileInfo, err error) error {
		if err != nil || !info.Mode().IsRegular() {
			return err
		}
		total++
		content, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(root, p)
		if len(content) > contentLimit {
			// Gitea parses only the first MiB, so a longer file is checked as Gitea sees it.
			fmt.Printf("NOTE %s: %d bytes, only the first %d are read\n", filepath.ToSlash(rel), len(content), contentLimit)
			content = content[:contentLimit]
		}
		check := func(name string, err error) bool {
			if err != nil {
				fmt.Printf("INVALID %s (%s): %v\n", filepath.ToSlash(rel), name, err)
				bad++
			}
			return err == nil
		}
		_, err = jobparser.ValidateWorkflowStatic(content)
		if !check("ValidateWorkflowStatic", err) {
			return nil
		}
		_, err = jobparser.ReadWorkflow(content)
		if !check("ReadWorkflow", err) {
			return nil
		}
		_, err = jobparser.Parse(content)
		check("Parse", err)
		return nil
	})
	if err != nil {
		fmt.Println("walk:", err)
		os.Exit(2)
	}
	if total != expected {
		fmt.Printf("MISMATCH: %d files checked, %d exported\n", total, expected)
		os.Exit(3)
	}
	fmt.Printf("checked %d workflow files, %d invalid\n", total, bad)
	if bad > 0 || total == 0 {
		os.Exit(1)
	}
}
