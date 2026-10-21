"""Display building demands without interpreting them as an oriented design check.

P is positive in compression, negative in tension; moments are report-component
magnitudes. Blank moments stay unknown. No resultant, sign, simultaneity, member
orientation, resistance factor, or pass/fail result is inferred from a report.
"""
import csv
from collections import defaultdict
import math
from pathlib import Path
import textwrap


FIELDS = ('id', 'floor', 'column', 'load_case', 'P_kN', 'M_major_kNm', 'M_minor_kNm', 'notes')
COLUMNS = ('interior', 'perimeter', 'corner')
LOAD_CASES = ('gravity', 'seismic')


def read_demands(path):
    rows, identities = [], set()
    with open(path, encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if not set(FIELDS).issubset(reader.fieldnames or ()):
            raise ValueError('Demand CSV requires columns: '+', '.join(FIELDS))
        for line, raw in enumerate(reader, 2):
            if None in raw or any(raw.get(key) is None for key in FIELDS):
                raise ValueError('Malformed demand CSV row at line %d' % line)
            row = {key: (raw.get(key) or '').strip() for key in FIELDS}
            if not row['id'] or row['id'] in identities:
                raise ValueError('Missing/duplicate demand id at CSV line %d' % line)
            identities.add(row['id'])
            if row['column'] not in COLUMNS or row['load_case'] not in LOAD_CASES:
                raise ValueError('Invalid column/load_case at demand CSV line %d' % line)
            try:
                row['floor'] = int(row['floor'])
                if row['floor'] < 1:
                    raise ValueError()
                for key in ('P_kN', 'M_major_kNm', 'M_minor_kNm'):
                    value = row[key]
                    row[key] = float(value) if value else None
                    if row[key] is None:
                        if key == 'P_kN':
                            raise ValueError()
                    elif not math.isfinite(row[key]) or key != 'P_kN' and row[key] < 0:
                        raise ValueError()
            except (ValueError, TypeError):
                raise ValueError('Demand CSV line %d needs a positive integer floor, finite signed P, '
                                 'and finite nonnegative moment magnitudes or blank unknown moments' % line) from None
            rows.append(row)
    if not rows:
        raise ValueError('Demand CSV contains no rows')
    return rows


def write_table(directory, rows):
    path = Path(directory)/'PM_demand_points.csv'
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(FIELDS)+['display_status', 'orientation'])
        writer.writeheader()
        for row in rows:
            unknown = row['M_major_kNm'] is None and row['M_minor_kNm'] is None
            writer.writerow(dict(row, display_status='axial_only_no_paired_M' if unknown else 'component_projection_only',
                                 orientation='unassigned'))
    return str(path)


def draw_overlay(ax, rows, scales=None):
    """Overlay separate report components; missing M never becomes a zero moment."""
    moment_scale, force_scale = scales or (1., 1.)
    colors = dict(interior='#762a83', perimeter='#006d2c', corner='#b2182b')
    plotted, axial_only = 0, sum(row['M_major_kNm'] is None and row['M_minor_kNm'] is None for row in rows)
    for column in COLUMNS:
        for load_case in LOAD_CASES:
            for key, component in (('M_major_kNm', 'major'), ('M_minor_kNm', 'minor')):
                selected = [row for row in rows if row['column'] == column and row['load_case'] == load_case
                            and row[key] is not None]
                if not selected:
                    continue
                x = [row[key]/moment_scale for row in selected]
                y = [row['P_kN']/force_scale for row in selected]
                style = dict(marker='x', linewidths=1.2) if component == 'minor' else dict(
                    marker='s' if load_case == 'gravity' else '^', edgecolors='black', linewidths=.6)
                ax.scatter(x, y, s=34, color=colors[column], zorder=7,
                           label='Demand: %s / %s / %s' % (column, load_case, component), **style)
                for index, row in enumerate(selected):
                    if component == 'minor' and row['M_major_kNm'] == row['M_minor_kNm']:
                        continue  # Equal component projections share the same floor label.
                    ax.annotate(str(row['floor']), (x[index], y[index]), textcoords='offset points',
                                xytext=(4, 4 if component == 'major' else -9), fontsize=6.5,
                                color=colors[column], zorder=8)
                plotted += len(selected)
    note = 'Demand components: orientation unassigned; display only.\nNumbers = storeys; report maxima may not be simultaneous.'
    if axial_only:
        note += '\n%d axial cases lack paired M: listed in PM_demand_points.csv, not plotted.' % axial_only
    ax.text(.0, 1.02, note, transform=ax.transAxes, ha='left', va='bottom', fontsize=6.5,
            zorder=9, bbox=dict(facecolor='white', edgecolor='0.8', alpha=.9))
    return dict(plotted_components=plotted, axial_only_cases=axial_only)


def _readable_groups(points, theta):
    from abaqus_pm_interaction import interaction_case, interaction_dataset, _float
    owned_axes = defaultdict(set)
    for row in points:
        axis = _float(row.get('bending_axis_deg'))
        if axis is not None:
            owned_axes[interaction_dataset(row), row.get('source', 'GMNIA'), interaction_case(row)].add(axis)
    groups = {}
    for row in points:
        m, p = _float(row.get('M_kNm')), _float(row.get('P_kN'))
        if m is None or p is None:
            continue
        axis = _float(row.get('bending_axis_deg'))
        case = interaction_case(row)
        key = (interaction_dataset(row), row.get('source', 'GMNIA'), case)
        if axis is not None and not math.isclose(axis, theta, abs_tol=1e-6):
            continue
        if axis is None and owned_axes[key] and not any(math.isclose(value, theta, abs_tol=1e-6) for value in owned_axes[key]):
            continue
        verified = (row.get('source', 'GMNIA') == 'GMNIA' and
                    (row.get('selection_kind') == 'VERIFIED_PEAK' or
                     not row.get('selection_kind') and row.get('outcome') in ('CRITERION_REACHED', 'CONTROLLED_TERMINATION')) and
                    str(row.get('capacity_verified', True)).lower() in ('true', '1'))
        if key not in groups:
            label = str(row.get('dataset_label') or Path(key[0]).name).split(' | ')[0]
            groups[key] = dict(label=label, case=case, source=key[1], dataset=key[0], values=[])
        groups[key]['values'].append(dict(m=m, p=p, psi=_float(row.get('pm_angle_deg')), verified=verified,
                                         kind=row.get('selection_kind', ''), model=row.get('model', '')))
    ordered = [groups[key] for key in sorted(groups)]
    cases = {group['case'] for group in ordered}
    labels = [group['label'] for group in ordered]
    palette = ('#0072b2', '#d55e00', '#009e73', '#cc79a7', '#8c564b', '#6f6f00', '#555555')
    established = {'HSS': ('#0072b2', 'D', '-.'), 'No plates': ('#d55e00', 'o', '-'),
                   'With plates': ('#009e73', 's', '--')}
    for index, group in enumerate(ordered):
        original = group['label']
        if len(cases) > 1:
            group['label'] += ' / '+group['case']
        if group['source'] != 'GMNIA':
            group['label'] += ' / '+group['source']
        if sum(label == original for label in labels) > 1:
            group['label'] += ' [%d]' % (index+1)
        color, marker, linestyle = established.get(original, (palette[index % len(palette)], 'o', '-'))
        if len(cases) > 1 and group['case'] != 'PERFECT':
            marker, linestyle = ('v', 'P', 'X')[index % 3], ('--', ':', '-.')[index % 3]
        group.update(color=color, marker=marker, linestyle=linestyle)
        group['values'].sort(key=lambda value: (value['psi'] if value['psi'] is not None else
                                                math.degrees(math.atan2(value['m'], value['p']))))
    return ordered


def _readable_references(report, theta):
    if not report:
        return None
    from abaqus_report_reference import moment_reference
    data = moment_reference(report, theta)
    return dict(moments=[(float(data[key]), label) for key, label in (('SFy_kNm', 'SFy'), ('ZFy_kNm', 'ZFy'))],
                force=float(report['AsFy_kN']), label=data['axis_label'])


def _readable_limits(groups, reference=None, components=()):
    moments = [v['m'] for group in groups for v in group['values']]+[d['m'] for d in components]+[0.]
    forces = [v['p'] for group in groups for v in group['values']]+[d['p'] for d in components]+[0.]
    if reference:
        moments += [value for value, label in reference['moments']]
        forces.append(reference['force'])
    return ((min(0., min(moments)*1.08), max(1., max(moments)*1.08)),
            (min(0., min(forces)*1.08), max(1., max(forces)*1.08)))


def _readable_curves(ax, groups, inset=False):
    for group in groups:
        values = group['values']
        for first, second in zip(values, values[1:]):
            gap = (first['psi'] is not None and second['psi'] is not None and
                   any(first['psi'] < angle < second['psi'] for angle in group.get('expected_angles', ())))
            ax.plot([first['m'], second['m']], [first['p'], second['p']], color=group['color'],
                    ls=(0, (1.2, 2.5)) if gap else group['linestyle'], lw=1.8 if inset else 2.2, zorder=3)
        for value in values:
            observed = value['kind'] in ('UNSELECTED_OBSERVED_MAXIMUM','UNVERIFIED_OBSERVED_MAXIMUM')
            # Inset markers MUST be clipped, including points outside the detail.
            line, = ax.plot(value['m'], value['p'], marker='x' if observed else group['marker'], ms=4.5 if inset else 6.3,
                           color=group['color'], mfc=group['color'] if value['verified'] else 'white',
                           mew=1.3, ls='none', clip_on=True, zorder=8 if value['verified'] else 5)
            line.set_gid('readable-observed-point' if observed else 'readable-fe-point')


def _readable_base(ax, limits, reference):
    ax.set(xlim=limits[0], ylim=limits[1], xlabel='Moment, M (kN m)',
           ylabel='Compression, P (kN)' if limits[1][0] >= 0 else 'Axial force, P (kN; compression positive)')
    ax.grid(True, color='#e7ebef', lw=.65)
    ax.set_axisbelow(True)
    ax.tick_params(direction='out', length=4, color='#536270')
    if limits[1][0] < 0:
        ax.axhline(0., color='#a0a8b0', lw=.8)
    if reference:
        for value, label in reference['moments']:
            ax.axvline(value, ls=':', lw=1., color='#a0a8b0', zorder=1)
        ax.axhline(reference['force'], ls='-.', lw=1., color='#a0a8b0', zorder=1)


def _readable_components(demands):
    components, unknown = [], 0
    for row in demands:
        has_moment = False
        for key, component in (('M_major_kNm', 'major'), ('M_minor_kNm', 'minor')):
            if row[key] is None:
                continue
            has_moment = True
            tag = ('g' if row['load_case'] == 'gravity' else 'e')+dict(interior='I', perimeter='P', corner='C')[row['column']]+str(row['floor'])
            if component == 'minor':
                tag = '' if row['M_major_kNm'] == row['M_minor_kNm'] else tag+'*'
            components.append(dict(m=row[key], p=row['P_kN'], tag=tag, column=row['column'],
                                   case=row['load_case'], component=component))
        unknown += not has_moment
    return components, unknown


def _readable_demand_scatter(ax, components):
    colors = dict(interior='#7b3294', perimeter='#006d2c', corner='#b2182b')
    for column in COLUMNS:
        for case in LOAD_CASES:
            for component in ('major', 'minor'):
                selected = [d for d in components if (d['column'], d['case'], d['component']) == (column, case, component)]
                if not selected:
                    continue
                style = dict(marker='x', linewidths=1.3) if component == 'minor' else dict(
                    marker='s' if case == 'gravity' else '^', edgecolors='white', linewidths=.65)
                ax.scatter([d['m'] for d in selected], [d['p'] for d in selected],
                           s=34 if component == 'minor' else 40, color=colors[column], zorder=6, **style)



def write_readable_plots(directory, points, sections, demands=None, report_reference=None,
                         publish_primary=False, analysis_audit=()):
    """Readable physical-coordinate views and source-separated demand panels.

    Lines pass through provisional points as comparison guides, not verified
    resistance envelopes. Marker fill describes evidence, not a design verdict. Missing rays
    are obtained only from the analysis plan; no assumed angular spacing, extra
    solved point, floor orientation or moment resultant is introduced.
    """
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.lines import Line2D
        from matplotlib.ticker import MaxNLocator
    except ImportError:
        print('matplotlib not available: supplemental readable plots skipped')
        return []
    from abaqus_pm_interaction import _float
    from abaqus_load_case import angle_token
    from abaqus_hss_aisc import (references_for_points, draw_references,
                                 write_reference_report, convexity_diagnostic, reference_notes)
    observations = [dict(row['observed_point'], source='GMNIA', dataset=row['dataset'],
                         dataset_label=row['dataset_label'])
                    for row in analysis_audit if row.get('observed_point')]
    displayed = list(points)+observations
    axes = {float(row['bending_axis_deg']) for row in displayed if
            row.get('bending_axis_deg') not in ('', None) and _float(row.get('M_kNm')) is not None}
    axes.update(float(data.get('neutral_axis_angle_deg', key.replace('p', '.'))) for key, data in sections.items())
    axes = sorted(axes) or [0.]
    components, unknown = _readable_components(demands or [])
    written, panels_for_overview = [], []
    written += write_reference_report(directory, displayed, axes)
    convexity = []
    settings = {'font.family': 'DejaVu Sans', 'font.size': 11, 'axes.titlesize': 12, 'axes.labelsize': 11,
                'axes.spines.top': False, 'axes.spines.right': False, 'axes.linewidth': .9,
                'xtick.labelsize': 10, 'ytick.labelsize': 10, 'savefig.facecolor': 'white',
                'svg.fonttype': 'none', 'pdf.fonttype': 42}
    def save(fig, stem):
        for extension in ('png', 'svg', 'pdf'):
            path = str(Path(directory)/(stem+'.'+extension))
            fig.savefig(path, dpi=220)
            written.append(path)
    def finish_axis(ax, limits, reference=None, hss=()):
        _readable_base(ax, limits, reference)
        draw_references(ax, hss, notes=False)
        ax.xaxis.set_major_locator(MaxNLocator(5))
        ax.yaxis.set_major_locator(MaxNLocator(6))
    def point_code(value):
        return ('O' if value['kind'] in ('UNSELECTED_OBSERVED_MAXIMUM','UNVERIFIED_OBSERVED_MAXIMUM') else
                'V' if value['verified'] else 'R' if value['kind']=='UNVERIFIED_PATH_RETRACE' else 'P')
    with plt.rc_context(settings):
        for theta in axes:
            groups = _readable_groups(displayed, theta)
            if not groups:
                continue
            for group in groups:
                group['expected_angles'] = sorted({float(row['pm_angle_deg']) for row in analysis_audit
                    if row['dataset']==group['dataset'] and row.get('source_case')==group['case'] and
                    row.get('pm_angle_deg') is not None and
                    (row.get('bending_axis_deg') is None or math.isclose(float(row['bending_axis_deg']),theta,abs_tol=1e-6))})
            reference = _readable_references(report_reference, theta)
            hss = references_for_points(displayed, theta)
            for group in groups:
                check = convexity_diagnostic([(v['m'],v['p']) for v in group['values']])
                convexity.append(dict(dataset=group['dataset'],source=group['source'],case=group['case'],
                                      angle_deg=theta,**check))
            limits = _readable_limits(groups, reference)
            panels_for_overview.append((theta, groups, limits, reference, hss))
            suffix = '_A'+angle_token(theta) if len(axes)>1 else ''
            known_angles = sorted({v['psi'] for group in groups for v in group['values'] if v['psi'] is not None} |
                                  {a for group in groups for a in group['expected_angles']})
            unassigned = [[v for v in group['values'] if v['psi'] is None] for group in groups]
            table_slots = [(psi,None) for psi in known_angles]+[(None,i) for i in range(max(map(len,unassigned)))]
            legend = [Line2D([], [], color=g['color'], marker=g['marker'], ls=g['linestyle'], lw=2,
                             mfc='white', label=textwrap.fill(g['label'], 28)) for g in groups]
            if any(v['kind'] in ('UNSELECTED_OBSERVED_MAXIMUM','UNVERIFIED_OBSERVED_MAXIMUM') for g in groups for v in g['values']):
                legend.append(Line2D([],[],color='#536270',marker='x',ls='none',
                                     label='Attained maximum (O)\ncapacity unverified'))
            height = max(8.8, 5.8+.36*len(table_slots)+.25*len(groups))
            fig = plt.figure(figsize=(max(14.2, 8.+2.2*len(groups)), height))
            try:
                fig.text(.065,.95,'P-M comparison | FE results',fontsize=21,weight='bold',color='#183247')
                cases = 'Perfect models' if all(g['case']=='PERFECT' for g in groups) else 'Selected model cases'
                provisional_count = sum(not v['verified'] for g in groups for v in g['values'])
                fig.text(.065,.905,'%s | neutral axis %g deg | %d recorded points (%d provisional / review)' %
                         (cases, theta, sum(len(g['values']) for g in groups), provisional_count),
                         fontsize=11,color='#536270')
                ax = fig.add_axes([.075,.19,.49,.64])
                finish_axis(ax,limits,reference,hss); _readable_curves(ax,groups)
                reference_handles = [line for line in ax.lines if str(line.get_gid() or '').startswith('hss-aisc-')]
                for line in reference_handles:
                    line.set_label(textwrap.fill(line.get_label(),34))
                if reference:
                    for value,label in reference['moments']:
                        ax.text(value,.98,label,transform=ax.get_xaxis_transform(),ha='center',va='top',
                                fontsize=9,color='#6b737b',bbox=dict(fc='white',ec='none',pad=1.))
                    ax.text(.98, reference['force'], 'AsFy / '+reference['label'],
                            transform=ax.get_yaxis_transform(),ha='right',va='bottom',fontsize=8,color='#6b737b')
                fig.legend(handles=legend+reference_handles,loc='upper left',bbox_to_anchor=(.60,.845),frameon=False,
                           fontsize=8.5 if reference_handles else 10.5,
                           ncol=max(2 if reference_handles else 1, math.ceil(len(groups)/3)),columnspacing=1.)
                fig.text(.61,.665,'RECORDED COORDINATES: P (kN) / M (kN m)',weight='bold',fontsize=10,color='#334155')
                table_ax=fig.add_axes([.605,.34,.365,.30]);table_ax.axis('off')
                cells=[]
                for psi,observation in table_slots:
                    row=['%g'%psi if psi is not None else 'obs %d'%(observation+1)]
                    for group_index,group in enumerate(groups):
                        matches=([v for v in group['values'] if v['psi'] is not None and math.isclose(v['psi'],psi,abs_tol=1e-7)]
                                 if psi is not None else unassigned[group_index][observation:observation+1])
                        row.append('\n'.join('%.1f / %.2f %s'%(v['p'],v['m'],point_code(v)) for v in matches) if matches else '--')
                    cells.append(row)
                table=table_ax.table(cellText=cells,colLabels=['Ray / obs']+[textwrap.fill(g['label'],16) for g in groups],
                                     cellLoc='center',loc='center',bbox=[0,0,1,1])
                table.auto_set_font_size(False);table.set_fontsize(max(6.,min(9.,28./(len(groups)+.5))))
                for (i,j),cell in table.get_celld().items():
                    cell.set_edgecolor('#e1e7ed');cell.set_linewidth(.6)
                    if i==0:cell.set_facecolor('#edf2f6');cell.set_text_props(weight='bold',color='#183247')
                    elif j>0:cell.set_text_props(color=groups[j-1]['color'])
                fig.text(.61,.315,'V: selected peak passing the current checks\nP: provisional selected point\nR: path retracing suspected; observed point only\nO / x: attained maximum; capacity unverified\n--: no recorded point; obs: ray angle unspecified',
                         va='top',fontsize=9.3,color='#334155',linespacing=1.6)
                missing=[]
                for group in groups:
                    absent=[a for a in group['expected_angles'] if not any(v['psi'] is not None and
                            math.isclose(v['psi'],a,abs_tol=1e-7)
                            for v in group['values'])]
                    if absent:missing.append(group['label']+': '+', '.join('%g'%a for a in absent)+' deg')
                side_notes=reference_notes(hss)
                if missing:side_notes.append('Missing recorded rays: '+'; '.join(missing)+' deg')
                if side_notes:
                    fig.text(.61,.185,'\n'.join(textwrap.fill(note,58) for note in side_notes),
                             va='top',fontsize=8,color='#743981')
                fig.text(.075,.095,'Lines connect all displayed results, including provisional points. They are comparison guides, not verified resistance envelopes.',
                         fontsize=9.5,color='#536270')
                fig.text(.075,.055,'V describes the current numerical checks, not independent physical validation or a design resistance.',fontsize=9.5,color='#536270')
                if any(c['status']=='NONCONVEX_SAMPLES' and c['angle_deg']==theta for c in convexity):
                    fig.text(.075,.13,'Nonconvex sampled domain: review required. No outer hull or capacity-increasing fit was applied.',
                             fontsize=8.5,color='#8b4c22')
                save(fig,'01_PM_models_readable'+suffix)
                if publish_primary:
                    save(fig,'PM_interaction_A'+angle_token(theta))
                    if len(axes)==1:save(fig,'PM_interaction')
            finally:plt.close(fig)
            if not demands:
                continue
            columns=[(column,key,title) for column,key,title in (
                ('interior','M_major_kNm','Interior / main component'),
                ('perimeter','M_major_kNm','Perimeter / main component'),
                ('corner','M_major_kNm','Corner / main component'),
                ('corner','M_minor_kNm','Corner / second component'))
                if any(d['column']==column and d[key] is not None for d in demands)]
            # Include unusual but valid minor-only demands without silently dropping them.
            for column in ('interior','perimeter'):
                if any(d['column']==column and d['M_minor_kNm'] is not None for d in demands):
                    columns.append((column,'M_minor_kNm',column.title()+' / second component'))
            if not columns:
                continue
            fig,panels=plt.subplots(2,len(columns),figsize=(max(12,4.75*len(columns)),10.2),squeeze=False)
            try:
                fig.subplots_adjust(left=.065,right=.98,bottom=.16,top=.75 if hss else .78,wspace=.36,hspace=.40)
                fig.text(.065,.95,'Building demands | separated P-M component views',fontsize=22,weight='bold',color='#183247')
                fig.text(.065,.91,'Neutral axis %g deg | numbers identify storeys; original coordinates are preserved.'%theta,fontsize=11,color='#536270')
                handles=legend+[Line2D([],[],marker='o',mfc='#692b7c',mec='white',ls='none',label='Demand (number = storey)')]+reference_handles
                fig.legend(handles=handles,loc='upper left',bbox_to_anchor=(.06,.875),ncol=min(len(handles),4 if hss else 5),frameon=False,fontsize=8.5 if hss else 10,columnspacing=2.)
                if hss:
                    fig.text(.065,.81,' '.join(reference_notes(hss)),fontsize=8,color='#743981')
                for i,case in enumerate(LOAD_CASES):
                    for j,(column,key,title) in enumerate(columns):
                        panel=panels[i,j]
                        selected=[d for d in demands if d['column']==column and d['load_case']==case and d[key] is not None]
                        physical=[dict(m=d[key],p=d['P_kN']) for d in selected]
                        finish_axis(panel,_readable_limits(groups,None,physical),hss=hss);_readable_curves(panel,groups)
                        panel.set_title(title+'\n'+('Gravity' if case=='gravity' else 'Earthquake'),loc='left',weight='bold',pad=10)
                        if selected:
                            panel.scatter([d[key] for d in selected],[d['P_kN'] for d in selected],c='#692b7c',s=45,edgecolors='white',lw=.7,zorder=6)
                            for d in selected:
                                panel.annotate(str(d['floor']),(d[key],d['P_kN']),xytext=(6,2),textcoords='offset points',
                                               fontsize=10,weight='bold',color='#692b7c',zorder=7)
                        else:panel.text(.5,.9,'No paired demand',transform=panel.transAxes,ha='center',color='#657687')
                fig.text(.065,.09,'Panel scales differ. Open markers / crosses: unverified points. Lines pass through them as comparison guides.',fontsize=10,color='#334155')
                fig.text(.065,.045,'Orientation unassigned: report components only; no floor pass/fail verdict. %d axial-only cases lack paired M and are omitted.'%unknown,fontsize=10,color='#536270')
                save(fig,'02_PM_demands_readable'+suffix)
            finally:plt.close(fig)
        if publish_primary and len(panels_for_overview)>1:
            columns=min(2,len(panels_for_overview));rows=math.ceil(len(panels_for_overview)/columns)
            fig,panels=plt.subplots(rows,columns,figsize=(8*columns,6*rows),squeeze=False,constrained_layout=True)
            try:
                for ax,(theta,groups,limits,reference,hss) in zip(panels.flat,panels_for_overview):
                    finish_axis(ax,limits,reference,hss);_readable_curves(ax,groups)
                    ax.set_title('FE results | neutral axis %g deg'%theta)
                    handles=[Line2D([],[],color=g['color'],marker=g['marker'],ls=g['linestyle'],mfc='white',label=g['label']) for g in groups]
                    if any(v['kind'] in ('UNSELECTED_OBSERVED_MAXIMUM','UNVERIFIED_OBSERVED_MAXIMUM') for g in groups for v in g['values']):
                        handles.append(Line2D([],[],color='#536270',marker='x',ls='none',label='Attained maximum; capacity unverified'))
                    handles += [line for line in ax.lines if str(line.get_gid() or '').startswith('hss-aisc-')]
                    ax.legend(handles=handles,fontsize=8,frameon=False)
                for ax in list(panels.flat)[len(panels_for_overview):]:ax.set_visible(False)
                save(fig,'PM_interaction')
            finally:plt.close(fig)
        reviews=[row for row in analysis_audit if row.get('path_review_required') and row.get('response_csv')]
        if reviews:
            fig,panels=plt.subplots(len(reviews),2,figsize=(13,max(5.8,3.5*len(reviews))),squeeze=False,constrained_layout=True)
            try:
                fig.suptitle('Response path review | load reduction alone does not verify collapse',fontsize=15,weight='bold')
                for index,record in enumerate(reviews):
                    with open(record['response_csv'],encoding='utf-8-sig',newline='') as stream:data=list(csv.DictReader(stream))
                    key=record['selection_response'];x=[float(d[key]) for d in data];y=[float(d['lpf']) for d in data]
                    frame=[int(d['frame']) for d in data];peak=max(range(len(y)),key=y.__getitem__)
                    response_label = {'extreme_fibre_shortening_mm': 'Extreme-fibre shortening (mm)',
                                      'shortening_mm': 'Axial shortening (mm)',
                                      'rotation_mrad': 'End rotation (mrad)'}.get(key, key.replace('_', ' '))
                    for ax,abscissa,label in ((panels[index,0],x,response_label),(panels[index,1],frame,'Stored frame')):
                        ax.plot(abscissa,y,'o-',color='#0072b2',ms=3)
                        ax.plot(abscissa[peak],y[peak],'o',mfc='white',mec='#b2182b',ms=7)
                        ax.set(xlabel=label,ylabel='Load proportionality factor',title=textwrap.fill(record['dataset_label']+' | '+record['model'],65))
                        ax.grid(color='#e7ebef')
                save(fig,'03_PM_path_checks')
            finally:plt.close(fig)
    import json
    diagnostic_path = Path(directory)/'PM_convexity_diagnostics.json'
    diagnostic_path.write_text(json.dumps(dict(schema='pm_convexity_screen_v1',groups=convexity),
                                          indent=2,allow_nan=False),encoding='utf-8')
    written.append(str(diagnostic_path))
    return written
