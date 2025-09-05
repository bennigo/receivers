#!/bin/bash


cat ~/.ssh/id_ed25519.pub | ssh ${1}@${2} "mkdir -p ~/.ssh && cat >>  ~/.ssh2/authorized_keys"
