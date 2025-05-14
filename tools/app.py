"""
Streamlit-based web interface for paper searching and filtering.
This module provides a user-friendly interface for searching and analyzing academic papers.
"""

import streamlit as st
import json
import os
import glob
from typing import List, Dict, Any, Optional
import logging
from extract import load_data, filter_data, count_results, SEARCH_MODE_AND, SEARCH_MODE_OR, DEFAULT_FIELDS
from key_fields_loader import load_conference_key_fields, get_available_conferences, get_conference_years, load_conference_categories

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

CONFERENCES = [name for name in os.listdir('../') if os.path.isdir(os.path.join('../', name)) and name != 'tools' and not name.startswith('.')]

DATA_SEARCH_MODES = ["All Papers", "Conference(s)"]

# Use Streamlit cache for expensive operations
@st.cache_data
def load_conference_data(conference_name: str) -> Optional[List[Dict[str, Any]]]:
    """
    Load conference data with dynamic year selection.
    
    Args:
        conference_name (str): Name of the conference to load data for
        
    Returns:
        Optional[List[Dict[str, Any]]]: Conference data if successful, None otherwise
    """
    # Base directories to search
    base_dir = "../"
    
    # Find directory containing conference data
    conf_dir = None
    possible_dir = os.path.join(base_dir, conference_name)
    if os.path.isdir(possible_dir):
        conf_dir = possible_dir
    
    if not conf_dir:
        st.error(f"Could not find directory for {conference_name}")
        return None
    
    # Find all conference JSON files
    pattern = os.path.join(conf_dir, f"{conference_name}*.json")
    json_files = glob.glob(pattern)
    
    if not json_files:
        st.error(f"No JSON files found for {conference_name}")
        return None
    
    # Sort files and get the latest one
    latest_file = sorted(json_files)[-1]
    
    try:
        with open(latest_file, encoding='utf-8') as f:
            data = json.load(f)
            # st.info(f"Loaded data from {os.path.basename(latest_file)}")
            return data
    except (FileNotFoundError, json.JSONDecodeError) as e:
        st.error(f"Error loading {os.path.basename(latest_file)}: {str(e)}")
        return None


def create_search_sidebar() -> Dict[str, Any]:
    """
    Create sidebar for search configuration.
    
    Returns:
        Dict[str, Any]: Dictionary containing search parameters
    """
    with st.sidebar:
        st.subheader("选择论文来源")
        data_search_mode = st.radio(
            "数据源:",
            DATA_SEARCH_MODES,
            help="选择您希望如何搜索论文",
            horizontal=False,
            index=None,
            label_visibility="collapsed"
        )
        
        st.header("搜索配置")
        keyword = st.text_input(
            "输入关键词:", 
            value="",
            help="多个关键词可以用逗号或空格分隔（例如：'retrieval agent' 或 'retrieval,agent'）。留空将显示所有结果。"
        )
        
        search_mode = st.radio(
            "关键词搜索模式:",
            [SEARCH_MODE_OR, SEARCH_MODE_AND],
            help=f"{SEARCH_MODE_OR}: 查找包含任一关键词的论文。{SEARCH_MODE_AND}: 查找包含所有关键词的论文。",
            horizontal=True
        )
        
        fields_to_search = st.multiselect(
            "选择要搜索的字段（多选）:",
            options=DEFAULT_FIELDS,
            default=None
        )
        
        st.subheader("其它选项")
        show_all_fields = st.checkbox(
            "显示全部字段", 
            value=False,
            help="勾选此项将显示论文的所有字段。"
        )
        
        include_rejected = st.checkbox(
            "包含被拒绝/撤回的论文", 
            value=False,
            help="勾选此项可包含被拒绝或撤回的论文。"
        )
        
    
    return {
        "keyword": keyword,
        "search_mode": search_mode,
        "fields_to_search": fields_to_search,
        "data_search_mode": data_search_mode,
        "show_all_fields": show_all_fields,
        "include_rejected": include_rejected,
    }


def load_data_source(data_search_mode: str) -> tuple:
    """
    Load data based on selected source mode.
    
    Args:
        data_search_mode (str): Type of data source to load
        
    Returns:
        tuple: (data, source, key_fields_filters) where data is the loaded data, 
               source is its description, and key_fields_filters contains any key field filters
    """
    data = None
    source = ""
    key_fields_filters = {}
    conference_categories = {}  # 存储每个会议的研究方向分类
    
    if data_search_mode == DATA_SEARCH_MODES[0]:  # All Papers
        # 加载所有会议的论文数据
        data = []
        loaded_conferences = []
        
        for conf in CONFERENCES:
            conf_data = load_conference_data(conf)
            if conf_data:
                for paper_item in conf_data: # Ensure each paper has its source conference
                    paper_item['source'] = conf
                data.extend(conf_data)
                loaded_conferences.append(conf)
        
        if data:
            st.session_state['data'] = data
            source = "All Papers (" + ", ".join(loaded_conferences) + ")"
            st.info(f"已加载 {len(loaded_conferences)} 个会议的 {len(data)} 篇论文")
            # Clear conference-specific filters when switching to All Papers
            if 'conference_categories' in st.session_state:
                del st.session_state['conference_categories']
            # key_fields_filters is returned by this function, so it will be an empty dict
            # for 'All Papers' mode, which is correct. We ensure no old values are passed
            # by re-initializing it at the start of the function.
        else:
            st.error("未能加载任何会议数据")
            source = "No Data"
    
    elif data_search_mode == DATA_SEARCH_MODES[1]: # Conference(s)
        conferences = st.multiselect("选择会议:", CONFERENCES)
        st.session_state['selected_conferences'] = conferences
        
        if conferences:
            # 加载所有选中会议的数据
            data = []
            for conf in conferences:
                conf_data = load_conference_data(conf)
                if conf_data:
                    for paper_item in conf_data: # Ensure each paper has its source conference
                        paper_item['source'] = conf
                    data.extend(conf_data)
                    # 加载会议的研究方向分类
                    categories_loaded = load_conference_categories(conf)
                    if categories_loaded:
                        conference_categories[conf] = categories_loaded
            
            st.session_state['data'] = data
            
            # 设置数据源描述
            if len(conferences) == 1:
                source = conferences[0]
            else:
                source = "+".join(conferences)
            
            # 在侧边栏添加筛选选项
            with st.sidebar:
                # 为每个会议添加研究方向筛选和关键字段筛选
                for conf in conferences:
                    st.subheader(conf.upper())
                    
                    # 添加研究方向筛选
                    categories_for_conf = conference_categories.get(conf, {})
                    if categories_for_conf:
                        category_options = list(categories_for_conf.keys())
                        if category_options:
                            selected_categories = st.multiselect(
                                f"研究方向:",
                                options=category_options,
                                default=[],
                                help="选择特定的研究方向进行筛选，可多选"
                            )
                            # 无论是否选择研究方向，都更新会话状态
                            if 'conference_categories' not in st.session_state:
                                st.session_state['conference_categories'] = {}
                            
                            # 如果用户取消了所有选择，则从会话状态中移除该会议的研究方向筛选
                            if not selected_categories and conf in st.session_state['conference_categories']:
                                del st.session_state['conference_categories'][conf]
                            # 如果有选择，则更新会话状态
                            elif selected_categories:
                                st.session_state['conference_categories'][conf] = selected_categories
                    
                    # 添加关键字段筛选
                    key_fields = load_conference_key_fields(conf)
                    
                    for field, values in key_fields.items():
                        if values:
                            field_key = f"{conf}_{field}"
                            selected = st.multiselect(
                                f"{field.capitalize()}:",
                                options=values,
                                default=[],
                                help=f"选择要筛选的 {field} 值。不选择则显示所有值。",
                                key=field_key
                            )
                            if selected:
                                if field not in key_fields_filters:
                                    key_fields_filters[field] = {}
                                key_fields_filters[field][conf] = selected
        else:
            data = None
            st.session_state['data'] = None
            source = ""
    
    return data, source, key_fields_filters


def display_search_results(data, source, search_params):
    """
    Filter data and display search results.
    
    Args:
        data: The data to search
        source: Source description of the data
        search_params: Dictionary of search parameters
    """
    keyword = search_params["keyword"]
    search_mode = search_params["search_mode"]
    fields_to_search = search_params["fields_to_search"]
    include_rejected = search_params["include_rejected"]
    key_fields_filters = search_params.get("key_fields_filters", {})
    show_all_fields = search_params.get("show_all_fields", False)
    
    conference_categories_selection = st.session_state.get('conference_categories', {})
    
    data = st.session_state.get('data')
    if source == '':
        st.warning("请选择会议")
        return
    if not data:
        st.error("无法加载数据，请检查。")
        return
        
    keywords_list = [k.strip() for k in keyword.replace(',', ' ').split() if k.strip()]

    if fields_to_search == [] and keywords_list != []:
        st.warning("请选择要搜索的字段")
        return
    if fields_to_search != [] and keywords_list == []:
        st.warning("请输入关键词")
        return

    if keywords_list:
        if len(keywords_list) > 1:
            if search_mode == SEARCH_MODE_OR:
                st.info(f"搜索包含任一关键词的论文: {', '.join(keywords_list)}")
            else:
                st.info(f"搜索包含所有关键词的论文: {', '.join(keywords_list)}")
        else:
            st.info(f"搜索包含关键词的论文: {keywords_list[0]}")
    else:
        st.info("未输入关键词，将显示所有符合筛选条件的论文。")
    
    if key_fields_filters:
        filter_descriptions = []
        for field, conf_values_dict in key_fields_filters.items():
            field_specific_descriptions = []
            for conf_name, values in conf_values_dict.items():
                if values:
                    str_values = [str(val) for val in values]
                    field_specific_descriptions.append(f"{conf_name} {field.capitalize()}: {', '.join(str_values)}")
            if field_specific_descriptions:
                 filter_descriptions.append(" | ".join(field_specific_descriptions))

        if filter_descriptions:
            st.info(f"应用的关键字段筛选: {'; '.join(filter_descriptions)}")
            
    if conference_categories_selection:
        display_messages = []
        for conf_name, selected_cats in conference_categories_selection.items():
            if selected_cats:
                display_messages.append(f"{conf_name} 研究方向: {', '.join(selected_cats)}")
        if display_messages:
            st.info(f"研究方向分类筛选: {' | '.join(display_messages)}")

    with st.spinner('正在处理数据...'):
        if keywords_list:
            status_filtered, filtered = filter_data(data, keyword, fields_to_search, search_mode, include_rejected)
        else:
            if include_rejected:
                status_filtered = data
                filtered = data
            else:
                status_filtered = [item for item in data if item.get('status') not in ['Withdraw', 'Reject', 'Desk Reject']]
                filtered = status_filtered
        
        if key_fields_filters and filtered:
            data_search_mode = search_params.get("data_search_mode", "")
            
            if data_search_mode == DATA_SEARCH_MODES[1]: # Conference(s)
                filtered_papers_after_key_fields = []
                for paper in filtered:
                    paper_conf = paper.get('source')
                    include_paper = True
                    
                    for field, conf_values_map in key_fields_filters.items():
                        if paper_conf in conf_values_map and conf_values_map[paper_conf]:
                            field_value_in_paper = paper.get(field)
                            selected_values_for_conf_field = conf_values_map[paper_conf]
                            
                            if field == 'award' and isinstance(field_value_in_paper, bool):
                                str_selected_values = [str(val).lower() for val in selected_values_for_conf_field]
                                if str(field_value_in_paper).lower() not in str_selected_values:
                                    include_paper = False
                                    break
                            elif field_value_in_paper not in selected_values_for_conf_field:
                                include_paper = False
                                break
                    
                    if include_paper:
                        filtered_papers_after_key_fields.append(paper)
                filtered = filtered_papers_after_key_fields
        
        data_search_mode = search_params.get("data_search_mode", "")
        if data_search_mode == DATA_SEARCH_MODES[1]: # Conference(s)
            if conference_categories_selection and filtered:
                filtered_papers_after_category = []
                for paper in filtered:
                    paper_conf = paper.get('source')
                    
                    if paper_conf not in conference_categories_selection or not conference_categories_selection[paper_conf]:
                        filtered_papers_after_category.append(paper)
                        continue
                    
                    selected_categories_for_conf = conference_categories_selection[paper_conf]
                    
                    actual_conf_categories_data = load_conference_categories(paper_conf)

                    if not actual_conf_categories_data:
                        filtered_papers_after_category.append(paper)
                        continue
                    
                    paper_ids_in_selected_cats = set()
                    for cat_name in selected_categories_for_conf:
                        if cat_name in actual_conf_categories_data:
                            paper_ids_in_selected_cats.update(actual_conf_categories_data[cat_name])
                    
                    if paper.get('id') in paper_ids_in_selected_cats:
                        filtered_papers_after_category.append(paper)
                
                filtered = filtered_papers_after_category

        counts = count_results(data, status_filtered, filtered, keyword, fields_to_search, search_mode)

        st.subheader("搜索统计")
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.metric("总论文数", len(data))
        with col2:
            st.metric("状态筛选后的论文" if include_rejected else "已接收的论文", counts['status_filtered_count'])
        with col3:
            st.metric("匹配结果", len(filtered))

        if filtered:
            st.subheader(f"找到 {len(filtered)} 篇匹配的论文")
            
            for paper in filtered:
                if 'source' not in paper:
                    paper['source'] = source
            
            if not show_all_fields:
                display_fields = ['title', 'status', 'track', 'abstract', 'site', 'keywords', 'primary_area', 'award', 'source', 'id']
                filtered_display = []
                for paper in filtered:
                    paper_display = {}
                    for field in display_fields:
                        if field in paper:
                            paper_display[field] = paper[field]
                    if 'id' not in paper_display and 'id' in paper :
                         paper_display['id'] = paper['id']
                    filtered_display.append(paper_display)
                
                st.info("当前只显示部分重要字段。如需查看全部字段，请在侧边栏勾选\"显示全部字段\"选项。")
                st.dataframe(filtered_display)
            else:
                st.dataframe(filtered)

            output_data = {
                "total_papers": len(data),
                "papers_after_status_filter": counts['status_filtered_count'],
                "matching_results": len(filtered),
                "filtered_papers": filtered
            }
            
            filename = f"filtered_results-{source.replace('+', '_')}"
            if keyword:
                filename += f"-{keyword.replace(' ', '_').replace(',', '_')}"
            
            st.download_button(
                label="下载结果 (JSON)",
                data=json.dumps(output_data, ensure_ascii=False, indent=2),
                file_name=f"{filename}.json",
                mime="application/json"
            )
        else:
            st.info("没有找到符合条件的论文。")


def main():
    """设置 Streamlit 界面并处理用户交互的主函数。"""
    st.set_page_config(page_title="论文搜索工具", layout="wide")


    # 从侧边栏获取搜索参数
    search_params = create_search_sidebar()

    if search_params['data_search_mode'] is None:
        st.warning("请选择论文来源")
        return

    data, source, key_fields_filters = load_data_source(search_params["data_search_mode"])
    
    # 将关键字段筛选添加到搜索参数中
    search_params["key_fields_filters"] = key_fields_filters

    # 搜索按钮和结果
    if st.button("搜索论文"):
        display_search_results(data, source, search_params)


if __name__ == "__main__":
    main()