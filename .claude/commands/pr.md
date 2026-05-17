Review pull request $1. Do NOT modify any local files, code, or tests. All output goes to PR comments only.

Use the format defined in `.claude/commands/review-format.md` — follow the "Initial Review Format" section exactly. Use the severity definitions and approval rules from that file.

## Step 1: Gather Context

1. Fetch the PR details (description, title, changed files, diff) using `gh`
2. Read the full diff of the PR to understand all changes

## Step 2: Code Review

Review all changed files. Only flag issues found in the actual diff. Use the severity definitions from `review-format.md` to classify each finding as Critical, Major, or Minor.

## Step 3: Post Review Comment

Post a single structured review comment on the PR following the "Initial Review Format" template from `review-format.md`. Include suggested code fixes only for **critical** issues.

## Step 4: Improve PR Description

If the PR description is missing or incomplete, update it to include:

- Summary of what changed and why
- List of affected apps/libraries
- Testing notes