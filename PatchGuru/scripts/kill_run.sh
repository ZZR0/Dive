pkill -f 'patchguru.SpecInfer'
pkill -f 'scripts/run_all_specinfer.py'
ps aux | grep -E 'run_all_specinfer|patchguru.SpecInfer' | grep -v grep
