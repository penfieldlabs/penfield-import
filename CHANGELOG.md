# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.1] - 2026-05-19

### Fixed

- Bulk relationship batch errors (409 conflict, 5xx server error) no longer mark the
  entire batch as failed. The tool falls back to individual creates, skipping duplicates
  and checkpointing after each item. A circuit breaker aborts the batch after 3
  consecutive server errors to avoid hammering a sick server. Fixed #2.

## [1.0.0] - 2026-04-13

Initial public release.
