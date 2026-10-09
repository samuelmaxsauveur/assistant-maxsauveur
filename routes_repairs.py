from flask import Blueprint, request, jsonify, render_template

import database
import repairs

repairs_bp = Blueprint('repairs', __name__)


@repairs_bp.route('/reparations')
def repairs_page():
    cases = database.get_repair_cases()
    show_closed = request.args.get('tout') == '1'
    if not show_closed:
        cases = [c for c in cases if c['status'] not in ('delivered_customer', 'cancelled')]
    return render_template('repairs.html', cases=cases, statuses=repairs.STATUSES,
                           status_labels=repairs.STATUS_LABELS, decisions=repairs.DECISIONS,
                           parcel_labels=repairs.PARCEL_LABELS, show_closed=show_closed)


@repairs_bp.route('/reparations/<int:case_id>', methods=['POST'])
def update_repair(case_id):
    try:
        case, synced = repairs.update_case(case_id, **(request.json or {}))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    return jsonify({'success': True, 'synced': synced,
                    'status_label': repairs.STATUS_LABELS[case['status']]})


@repairs_bp.route('/reparations/refresh', methods=['POST'])
def refresh_repairs():
    """Run the hourly Wing check now, then mirror every file to the sheet."""
    try:
        moved = repairs.refresh_from_wing()
    except Exception as e:
        return jsonify({'success': False, 'error': f"Wing : {e}"}), 502
    synced = sum(repairs.push_to_sheet(c) for c in database.get_repair_cases())
    return jsonify({'success': True, 'moved': moved, 'synced': synced})
