#!/usr/bin/env python3
"""
Apply v3.0 improvements to n8n Forex Trading System workflows.
Reads standalone .js files and injects them as n8n nodes into workflow JSONs.
"""

import json
import copy
import sys

WORKFLOW_DIR = "/home/felix/Public/n8n/forex"

def read_js_file(filename):
    """Read a .js file and return its content as a string."""
    filepath = f"{WORKFLOW_DIR}/{filename}"
    try:
        with open(filepath, 'r') as f:
            return f.read()
    except FileNotFoundError:
        print(f"⚠️ File not found: {filepath}")
        return None

def read_workflow(filename):
    """Read a workflow JSON file."""
    filepath = f"{WORKFLOW_DIR}/{filename}"
    with open(filepath, 'r') as f:
        return json.load(f)

def write_workflow(data, filename):
    """Write a workflow JSON file."""
    filepath = f"{WORKFLOW_DIR}/{filename}"
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"✅ Written: {filename}")

def find_node_by_name(workflow, name):
    """Find a node by its name in the workflow."""
    for node in workflow.get('nodes', []):
        if node.get('name') == name:
            return node
    return None

def get_max_position(workflow):
    """Get the maximum x position in the workflow to place new nodes."""
    max_x = 0
    for node in workflow.get('nodes', []):
        pos = node.get('position', [0, 0])
        x = pos[0] if isinstance(pos, list) else pos
        if x > max_x:
            max_x = x
    return max_x

def create_code_node(name, js_code, position, node_id=None):
    """Create an n8n code node."""
    import uuid
    return {
        "parameters": {
            "jsCode": js_code
        },
        "id": node_id or str(uuid.uuid4()),
        "name": name,
        "type": "n8n-nodes-base.code",
        "position": position,
        "typeVersion": 2
    }

def find_connection_to_node(workflow, target_name):
    """Find which nodes connect TO the target node and return their names + output indices."""
    connections = []
    for node in workflow.get('nodes', []):
        for conn in workflow.get('connections', {}).get(node['name'], {}):
            for output_idx, targets in enumerate(workflow['connections'][node['name']].get(conn, [])):
                for target in targets:
                    if target.get('node') == target_name:
                        connections.append((node['name'], conn, output_idx))
    return connections

def update_node_code(workflow, node_name, new_js_code):
    """Update the JavaScript code of an existing node."""
    node = find_node_by_name(workflow, node_name)
    if node and 'jsCode' in node.get('parameters', {}):
        node['parameters']['jsCode'] = new_js_code
        print(f"  ✅ Updated node: {node_name}")
        return True
    print(f"  ⚠️ Could not update node: {node_name}")
    return False

def add_node_to_workflow(workflow, node):
    """Add a new node to the workflow."""
    if 'nodes' not in workflow:
        workflow['nodes'] = []
    workflow['nodes'].append(node)
    print(f"  ✅ Added node: {node['name']}")

def add_connection(workflow, from_node, from_type, to_node, to_type="main", from_output=0, to_input=0):
    """Add a connection between two nodes."""
    if 'connections' not in workflow:
        workflow['connections'] = {}
    
    if from_node not in workflow['connections']:
        workflow['connections'][from_node] = {}
    if from_type not in workflow['connections'][from_node]:
        workflow['connections'][from_node][from_type] = []
    
    # Ensure the output array is long enough
    while len(workflow['connections'][from_node][from_type]) <= from_output:
        workflow['connections'][from_node][from_type].append([])
    
    workflow['connections'][from_node][from_type][from_output].append({
        "node": to_node,
        "type": to_type,
        "index": to_input
    })
    print(f"  ✅ Connection: {from_node} -> {to_node}")

def main():
    print("=" * 60)
    print("  APPLYING v3.0 IMPROVEMENTS TO FOREX TRADING SYSTEM")
    print("=" * 60)
    
    # ========================================
    # 1. UPDATE multi-agente-profesional-CORRECTED.json
    # ========================================
    print("\n📋 Processing: multi-agente-profesional-CORRECTED.json")
    workflow = read_workflow("multi-agente-profesional-CORRECTED.json")
    
    # --- 1a. Add Risk Manager v3.0 node ---
    print("\n  [1/5] Adding Risk Manager v3.0...")
    risk_manager_code = read_js_file("Risk-Manager-v3.js")
    if risk_manager_code:
        # Find position: between "Check Action" (TRUE output) and "Preparar Orden"
        check_action = find_node_by_name(workflow, "Check Action")
        preparar_orden = find_node_by_name(workflow, "Preparar Orden")
        
        if preparar_orden:
            pos = preparar_orden.get('position', [816, 272])
            new_pos = [pos[0] - 200, pos[1]]
        else:
            new_pos = [600, 272]
        
        risk_node = create_code_node(
            "Risk Manager v3.0",
            risk_manager_code,
            new_pos,
            "risk-manager-v3-001"
        )
        add_node_to_workflow(workflow, risk_node)
        
        # Wire it: Check Action (true) -> Risk Manager -> Preparar Orden
        # First, remove the direct connection Check Action -> Preparar Orden
        if 'connections' in workflow and 'Check Action' in workflow.get('connections', {}):
            main_conns = workflow['connections']['Check Action'].get('main', [])
            if main_conns and len(main_conns) > 0:
                # Keep only the TRUE branch connection, redirect it through Risk Manager
                workflow['connections']['Check Action']['main'][0] = [{
                    "node": "Risk Manager v3.0",
                    "type": "main",
                    "index": 0
                }]
                print(f"  ✅ Re-routed: Check Action (true) -> Risk Manager v3.0")
        
        # Wire Risk Manager -> Preparar Orden
        add_connection(workflow, "Risk Manager v3.0", "main", "Preparar Orden")
    
    # --- 1b. Update Analizar Pares with v3.0 indicators ---
    print("\n  [2/5] Checking Analizar Pares v3.0...")
    # The v3 code for Analizar Pares would need to be in a separate .js file
    # For now, we'll note this needs to be done manually or create the file
    
    # --- 1c. Update Memory Manager with v3.0 circuit breaker ---
    print("\n  [3/5] Checking Memory Manager v3.0...")
    # Same - needs v3 code file
    
    # --- 1d. Update Preparar Orden with ATR-based SL/TP ---
    print("\n  [4/5] Checking Preparar Orden v3.0...")
    
    # --- 1e. Add Voting Engine v3.0 node ---
    print("\n  [5/5] Adding Voting Engine v3.0...")
    voting_code = read_js_file("Voting-Engine-v3.js")
    if voting_code:
        # The Voting Engine replaces Agente Estratega in the main workflow
        # or sits after the agents merge
        agregar_estratega = find_node_by_name(workflow, "Agente Estratega")
        if agregar_estratega:
            pos = agregar_estratega.get('position', [816, 272])
        else:
            pos = [816, 272]
        
        voting_node = create_code_node(
            "Voting Engine v3.0",
            voting_code,
            [pos[0] - 200, pos[1]],
            "voting-engine-v3-001"
        )
        add_node_to_workflow(workflow, voting_node)
        print(f"  ✅ Added Voting Engine v3.0")
    
    write_workflow(workflow, "multi-agente-profesional-CORRECTED.json")
    
    # ========================================
    # 2. UPDATE jetson-CORRECTED.json
    # ========================================
    print("\n📋 Processing: jetson-CORRECTED.json")
    jetson = read_workflow("jetson-CORRECTED.json")
    
    # Check for Volume Analyzer
    volume_code = read_js_file("Volume-Analyzer-v3.js")
    if volume_code:
        print("\n  Adding Volume Analyzer v3.0 to jetson workflow...")
        agente_tecnico = find_node_by_name(jetson, "Agente Técnico")
        if agente_tecnico:
            pos = agente_tecnico.get('position', [-176, 32])
            volume_node = create_code_node(
                "Volume Analyzer v3.0",
                volume_code,
                [pos[0] - 200, pos[1]],
                "volume-analyzer-v3-001"
            )
            add_node_to_workflow(jetson, volume_node)
            add_connection(jetson, "Volume Analyzer v3.0", "main", "Agente Técnico")
    
    write_workflow(jetson, "jetson-CORRECTED.json")
    
    print("\n" + "=" * 60)
    print("  v3.0 IMPROVEMENTS APPLIED")
    print("=" * 60)
    print("\n📌 NOTES:")
    print("   - Risk Manager v3.0 node added to main workflow")
    print("   - Voting Engine v3.0 node added to main workflow")
    print("   - Volume Analyzer v3.0 added to jetson workflow")
    print("   - Existing nodes need manual code replacement (see IMPLEMENTACION-V3-GUIA.md)")
    print("\n⚠️  To fully apply v3.0:")
    print("   1. Open each workflow in n8n UI")
    print("   2. Verify node connections are correct")
    print("   3. For Analizar Pares, Memory Manager, Preparar Orden:")
    print("      → Open the node and paste v3.0 code from IMPLEMENTACION-V3-GUIA.md")
    print("   4. Test with manual execution before activating schedule")

if __name__ == "__main__":
    main()
