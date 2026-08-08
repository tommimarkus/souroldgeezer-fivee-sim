#!/usr/bin/env python3
"""Generate Copilot-native agent profiles from canonical role descriptions."""

import json
import re
import sys
from pathlib import Path
from typing import Any

COPILOT_TOOL_MAP = {
    "typical-player": [],
    "game-master": ["read"],
    "adventure-prep": ["read", "edit"],
    "encounter-sim": ["execute", "read"],
    "play-mechanics": ["execute", "read"],
    "play-controller": ["agent", "execute", "read", "edit"],
}


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter and body from markdown file."""
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)$', text, re.DOTALL)
    if not match:
        return {}, text
    
    fm_text = match.group(1)
    body = match.group(2)
    
    frontmatter = {}
    for line in fm_text.split('\n'):
        if ':' in line:
            key, value = line.split(':', 1)
            key = key.strip()
            value = value.strip()
            frontmatter[key] = value
    
    return frontmatter, body


def generate_copilot_profile(
    role_name: str,
    canonical_path: Path,
    output_path: Path,
) -> None:
    """Generate a Copilot agent profile from a canonical role."""
    canonical_text = canonical_path.read_text(encoding='utf-8')
    canonical_fm, canonical_body = parse_frontmatter(canonical_text)
    
    description = canonical_fm.get('description', '')
    tools = COPILOT_TOOL_MAP.get(role_name, [])
    
    copilot_frontmatter = {
        'name': role_name,
        'description': description,
    }
    
    if tools:
        copilot_frontmatter['tools'] = tools
    
    fm_lines = ['---']
    for key in ['name', 'description', 'tools']:
        if key in copilot_frontmatter:
            value = copilot_frontmatter[key]
            if isinstance(value, list):
                if value:
                    fm_lines.append(f'{key}: {json.dumps(value)}')
            else:
                fm_lines.append(f'{key}: {value}')
    fm_lines.append('---')
    fm_lines.append('')
    
    output_content = '\n'.join(fm_lines) + canonical_body
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output_content, encoding='utf-8')


def generate_content(role_name: str, canonical_path: Path) -> str:
    """Generate profile content without writing (for --check mode)."""
    canonical_text = canonical_path.read_text(encoding='utf-8')
    canonical_fm, canonical_body = parse_frontmatter(canonical_text)
    
    description = canonical_fm.get('description', '')
    tools = COPILOT_TOOL_MAP.get(role_name, [])
    
    copilot_frontmatter = {
        'name': role_name,
        'description': description,
    }
    
    if tools:
        copilot_frontmatter['tools'] = tools
    
    fm_lines = ['---']
    for key in ['name', 'description', 'tools']:
        if key in copilot_frontmatter:
            value = copilot_frontmatter[key]
            if isinstance(value, list):
                if value:
                    fm_lines.append(f'{key}: {json.dumps(value)}')
            else:
                fm_lines.append(f'{key}: {value}')
    fm_lines.append('---')
    fm_lines.append('')
    
    return '\n'.join(fm_lines) + canonical_body


def main() -> int:
    """Main entry point."""
    if len(sys.argv) > 1 and sys.argv[1] not in ('--check', '--generate'):
        print(f"Usage: {sys.argv[0]} [--check|--generate]", file=sys.stderr)
        print("  --check:    Verify generated files match current state (default)", file=sys.stderr)
        print("  --generate: Write/overwrite generated files", file=sys.stderr)
        return 1
    
    mode = sys.argv[1] if len(sys.argv) > 1 else '--check'
    
    script_path = Path(__file__).resolve()
    plugin_root = script_path.parent.parent
    
    agents_path = plugin_root / 'souroldgeezer-fivee-sim' / 'agents'
    copilot_agents_path = plugin_root / 'souroldgeezer-fivee-sim' / 'copilot' / 'agents'
    
    if not agents_path.is_dir():
        print(f"Error: canonical agents directory not found at {agents_path}", file=sys.stderr)
        return 1
    
    roles = [
        'typical-player',
        'game-master',
        'adventure-prep',
        'encounter-sim',
        'play-mechanics',
        'play-controller',
    ]
    
    all_match = True
    
    for role_name in roles:
        canonical_file = agents_path / f'{role_name}.md'
        output_file = copilot_agents_path / f'{role_name}.agent.md'
        
        if not canonical_file.exists():
            print(f"Error: canonical role file not found: {canonical_file}", file=sys.stderr)
            return 1
        
        if mode == '--generate':
            generate_copilot_profile(role_name, canonical_file, output_file)
            print(f"Generated: {output_file.relative_to(plugin_root)}")
        else:
            if not output_file.exists():
                print(f"Missing: {output_file.relative_to(plugin_root)}")
                all_match = False
            else:
                temp_content = generate_content(role_name, canonical_file)
                existing_content = output_file.read_text(encoding='utf-8')
                if temp_content != existing_content:
                    print(f"Changed: {output_file.relative_to(plugin_root)}")
                    all_match = False
    
    if mode == '--check' and not all_match:
        print("\nRun with --generate to create/update agent profiles", file=sys.stderr)
        return 1
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
