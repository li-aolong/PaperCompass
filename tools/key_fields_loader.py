"""
加载会议关键字段信息的工具模块。
"""

import json
import os
import logging
from typing import Dict, List, Any, Optional

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 路径配置
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
KEY_INFOS_DIR = os.path.join(TOOLS_DIR, "key_infos")

def load_conference_key_fields(conference: str, year: Optional[str] = None) -> Dict[str, List[str]]:
    """
    加载指定会议的关键字段信息。
    
    Args:
        conference (str): 会议名称
        year (Optional[str]): 会议年份，如果不指定则加载最新的
        
    Returns:
        Dict[str, List[str]]: 关键字段及其可能的值
    """
    conf_dir = os.path.join(KEY_INFOS_DIR, conference)
    
    if not os.path.exists(conf_dir):
        logger.warning(f"会议目录不存在: {conf_dir}")
        return {}
    
    # 查找会议JSON文件
    json_files = [f for f in os.listdir(conf_dir) if f.endswith('.json')]
    
    if not json_files:
        logger.warning(f"未找到会议JSON文件: {conf_dir}")
        return {}
    
    # 如果指定了年份，尝试找到对应的文件
    target_file = None
    if year:
        for file in json_files:
            if year in file:
                target_file = file
                break
    
    # 如果没有指定年份或未找到对应年份的文件，使用最新的
    if not target_file:
        target_file = sorted(json_files)[-1]
    
    file_path = os.path.join(conf_dir, target_file)
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            key_fields = json.load(f)
        
        # 处理award字段，确保值是字符串类型
        if 'award' in key_fields:
            key_fields['award'] = [str(val) for val in key_fields['award']]
        
        # 移除categories字段，这个字段由load_conference_categories函数处理
        if 'categories' in key_fields:
            del key_fields['categories']
        
        return key_fields
    except Exception as e:
        logger.error(f"加载关键字段信息失败: {file_path}, 错误: {str(e)}")
        return {}

def get_available_conferences() -> List[str]:
    """
    获取所有可用的会议列表。
    
    Returns:
        List[str]: 可用会议列表
    """
    if not os.path.exists(KEY_INFOS_DIR):
        return []
    
    return [d for d in os.listdir(KEY_INFOS_DIR) if os.path.isdir(os.path.join(KEY_INFOS_DIR, d))]

def load_conference_categories(conference: str, year: Optional[str] = None) -> Dict[str, List[str]]:
    """
    加载指定会议的研究方向分类信息。
    
    Args:
        conference (str): 会议名称
        year (Optional[str]): 会议年份，如果不指定则加载最新的
        
    Returns:
        Dict[str, List[str]]: 研究方向分类及其对应的论文ID列表
    """
    conf_dir = os.path.join(KEY_INFOS_DIR, conference)
    
    if not os.path.exists(conf_dir):
        logger.warning(f"会议目录不存在: {conf_dir}")
        return {}
    
    # 查找会议JSON文件
    json_files = [f for f in os.listdir(conf_dir) if f.endswith('.json')]
    
    if not json_files:
        logger.warning(f"未找到会议JSON文件: {conf_dir}")
        return {}
    
    # 如果指定了年份，尝试找到对应的文件
    target_file = None
    if year:
        for file in json_files:
            if year in file:
                target_file = file
                break
    
    # 如果没有指定年份或未找到对应年份的文件，使用最新的
    if not target_file:
        target_file = sorted(json_files)[-1]
    
    file_path = os.path.join(conf_dir, target_file)
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            key_fields = json.load(f)
        
        # 获取categories字段
        if 'categories' in key_fields:
            return key_fields['categories']
        
        return {}
    except Exception as e:
        logger.error(f"加载研究方向分类信息失败: {file_path}, 错误: {str(e)}")
        return {}

def get_conference_years(conference: str) -> List[str]:
    """
    获取指定会议的所有可用年份。
    
    Args:
        conference (str): 会议名称
        
    Returns:
        List[str]: 可用年份列表
    """
    conf_dir = os.path.join(KEY_INFOS_DIR, conference)
    
    if not os.path.exists(conf_dir):
        return []
    
    # 查找会议JSON文件并提取年份
    years = []
    for file in os.listdir(conf_dir):
        if file.endswith('.json'):
            # 从文件名中提取年份，例如 "iclr2025.json" -> "2025"
            try:
                year = ''.join(filter(str.isdigit, file))
                if year:
                    years.append(year)
            except:
                pass
    
    return sorted(years)
